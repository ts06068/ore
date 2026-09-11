"""Cancelable, credential-free JSON programs in an ordinary isolated container.

Never silently falls back to executing generated code in the coordinator. Image
IDs are resolved on registration so retries cannot change the runtime unnoticed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from .policy import AccessDenied

class SandboxTerminationUnconfirmed(asyncio.CancelledError):
    termination_confirmed = False

    def __init__(self, container_name):
        super().__init__('Sandbox termination has not been confirmed')
        self.container_name = container_name


class ProgramFailed(ValueError):
    def __init__(self, status, stderr):
        super().__init__(f'Program exited with status {status}; no verified output')
        self.stderr = stderr  # private diagnostics; never part of public events


MAX_OUTPUT = 8_000_000

def resource_mode():
    mode = os.environ.get('ORE_CODE_RESOURCE_MODE', 'cgroup')
    if mode not in ('cgroup', 'rlimit'): raise AccessDenied('Unknown code sandbox resource mode')
    return mode
DEFAULT_IMAGES = {'python': 'ore-executor:0.2.0rc1', 'javascript': 'node:24-alpine'}


async def _command(*args, timeout=15):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode: raise AccessDenied('Sandbox runtime command failed: ' + err.decode(errors='replace')[:300])
        return out
    finally:
        if process.returncode is None:
            process.kill(); await process.wait()


async def resolve_image(language):
    if language not in DEFAULT_IMAGES: raise ValueError('Unsupported program language')
    docker = shutil.which('docker')
    if not docker: raise AccessDenied('Docker is required for generated programs; no host-code fallback is allowed')
    image = os.environ.get('ORE_CODE_' + language.upper() + '_IMAGE', DEFAULT_IMAGES[language])
    data = json.loads(await _command(docker, 'image', 'inspect', image))
    ident = data[0]['Id']
    if not ident.startswith('sha256:'): raise AccessDenied('Image has no immutable identity')
    return ident


async def sandbox_availability():
    result = {'backend': 'docker', 'languages': {}, 'network': 'none', 'resource_mode': resource_mode()}
    for language in DEFAULT_IMAGES:
        try: result['languages'][language] = {'available': True, 'image': await resolve_image(language)}
        except (AccessDenied, OSError, TimeoutError) as exc: result['languages'][language] = {'available': False, 'reason': str(exc)[:300]}
    return result


async def ensure_container_stopped(name):
    """Confirm Docker state; a dead CLI or failed remove is not termination proof."""
    if not name.startswith('ore-code-') or not name[9:].isalnum():
        return False
    docker = shutil.which('docker')
    if not docker:
        return False
    try:
        await _command(docker, 'rm', '-f', name, timeout=10)
    except (Exception,):
        pass
    try:
        # A successful daemon query with no exact name is evidence of absence.
        rows = (await _command(docker, 'ps', '-a', '--filter', 'name=^/' + name + '$',
                               '--format', '{{.Names}} {{.State}}', timeout=10)).decode().splitlines()
        return not any(row.split()[0] == name and row.split()[-1] in ('running', 'restarting', 'paused') for row in rows if row.split())
    except (Exception,):
        return False


async def run_program(program, value, *, timeout=30, staging_root=None):
    docker = shutil.which('docker')
    if not docker: raise AccessDenied('Docker sandbox is unavailable')
    if not 0 < timeout <= 120: raise ValueError('Invalid sandbox deadline')
    name = 'ore-code-' + uuid.uuid4().hex
    source = program['source']
    language = program['language']
    if program.get('network') != 'none' or not program['image'].startswith('sha256:'):
        raise AccessDenied('Program requires a pinned isolated runtime')
    payload = json.dumps(value, ensure_ascii=False).encode()
    if len(payload) > MAX_OUTPUT: raise ValueError('Program input exceeds transfer limit')
    # Docker reads files on its host. ORE_CODE_HOST_ROOT can map a shared staging
    # volume; never infer a mapping that could expose another host directory.
    root = os.environ.get('ORE_CODE_STAGING_ROOT') or staging_root
    if root: Path(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ore-code-', dir=root) as folder:
        directory = Path(folder); directory.chmod(0o755)
        path = directory / ('program.py' if language == 'python' else 'program.js')
        path.write_text(source); path.chmod(0o444)
        host = os.environ.get('ORE_CODE_HOST_ROOT')
        mount = str(Path(host) / directory.name) if host and root else str(directory.resolve())
        command = [docker, 'run', '--name', name, '--label', 'ore.role=code', '--label', 'ore.job=' + str(program.get('job_id', 'isolated-test')), '--rm', '-i', '--read-only', '--network', 'none',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', '64',
                   '--ulimit', 'cpu=120:120', '--ulimit', 'fsize=8388608:8388608', '--user', '65534:65534',
                   '--tmpfs', '/tmp:rw,nosuid,noexec,size=67108864', '--workdir', '/work',
                   '--mount', f'type=bind,src={mount},dst=/work,readonly']
        mode = program.get('resource_mode', 'cgroup')
        if mode == 'cgroup':
            command += ['--memory', '512m', '--cpus', '1', '--entrypoint', 'python3' if language == 'python' else 'node', program['image']]
            command += ['-I', '/work/program.py'] if language == 'python' else ['/work/program.js']
        elif mode == 'rlimit':
            # Explicit portable mode for hosts whose cgroups have no domain
            # controllers. These are per-process address-space limits, not an
            # aggregate container memory/CPU-share claim.
            if language == 'python':
                wrapper = 'import resource,runpy;resource.setrlimit(resource.RLIMIT_AS,(536870912,536870912));runpy.run_path("/work/program.py",run_name="__main__")'
                command += ['--entrypoint', 'python3', program['image'], '-I', '-c', wrapper]
            else:
                wrapper = 'ulimit -v 2097152; exec node --jitless --disable-wasm-trap-handler --max-old-space-size=128 /work/program.js'
                command += ['--entrypoint', '/bin/sh', program['image'], '-c', wrapper]
        else:
            raise AccessDenied('Unsupported sandbox resource mode')
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def bounded(stream):
            parts = []; total = 0
            while chunk := await stream.read(65536):
                total += len(chunk)
                if total > MAX_OUTPUT: raise ValueError('Sandbox output exceeds transfer limit')
                parts.append(chunk)
            return b''.join(parts)
        async def exchange():
            process.stdin.write(payload); await process.stdin.drain(); process.stdin.close()
            output, error = await asyncio.gather(bounded(process.stdout), bounded(process.stderr))
            await process.wait()
            if process.returncode:
                # Program stderr may contain task input; keep it out of public logs.
                if process.returncode == 125 and error.startswith(b'docker:'):
                    raise AccessDenied('Sandbox could not start: ' + error.decode(errors='replace')[:700])
                raise ProgramFailed(process.returncode, error.decode(errors='replace'))
            try: return json.loads(output)
            except (ValueError, UnicodeDecodeError) as exc: raise ValueError('Program must emit exactly one JSON result') from exc
        try:
            output = await asyncio.wait_for(exchange(), timeout)
            return {'output': output, 'image': program['image'], 'isolation': 'docker', 'network': 'none', 'resource_mode': mode, 'container_name': name}
        finally:
            async def cleanup():
                confirmed = await ensure_container_stopped(name)
                if process.returncode is None: process.kill()
                await process.wait()
                if not confirmed:
                    raise SandboxTerminationUnconfirmed(name)
            task = asyncio.create_task(cleanup())
            try: await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
