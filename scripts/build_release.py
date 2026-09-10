#!/usr/bin/env python3
"""Build local release artifacts and verify clean installation. Never publishes.

Requires Python 3.12+, uv, and Node 24/npm (or Docker). ORE_DOCKER_WORKSPACE
can specify the repository path visible to a remote/host Docker daemon.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from datetime import datetime, timezone
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NODE_IMAGE = 'node:24-alpine@sha256:50c8e8ca1d27439048670df5883f32d57cf81cff6233222c893fd0d9884cbd81'


def run(command: list[str], cwd: Path = ROOT, *, capture: bool = False) -> str:
    print('+ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout or ''


def npm(directory: str, *args: str) -> str:
    executable = shutil.which('npm')
    if executable:
        return run([executable, *args], ROOT / directory)
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('Install Node 24/npm or Docker to build the console and SDK.')
    mount = os.environ.get('ORE_DOCKER_WORKSPACE', str(ROOT))
    command = [docker, 'run', '--rm']
    if hasattr(os, 'getuid'):
        command += ['--user', f'{os.getuid()}:{os.getgid()}']
    command += ['-e', 'NPM_CONFIG_CACHE=/tmp/ore-npm-cache', '-v', f'{mount}:/repo',
                '-w', f'/repo/{directory}', NODE_IMAGE, 'npm', *args]
    return run(command)


def sdk_install_smoke(tarball: Path) -> None:
    node = shutil.which('node')
    docker = shutil.which('docker')
    native = bool(node and shutil.which('npm'))
    package = str(tarball) if native else f'/repo/dist/{tarball.name}'
    script = """const fs=require('node:fs');const os=require('node:os');const path=require('node:path');
const {execFileSync}=require('node:child_process');const {pathToFileURL}=require('node:url');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'ore-sdk-install-'));
(async()=>{try{execFileSync('npm',['install','--prefix',temp,'--ignore-scripts','--no-audit','--no-fund',PACKAGE],{stdio:'inherit'});
const sdk=await import(pathToFileURL(path.join(temp,'node_modules/@ore/sdk/dist/index.js')).href);
if(typeof sdk.OreClient!=='function'||typeof sdk.parseEventStream!=='function')throw Error('SDK exports missing');
console.log('SDK_CLEAN_INSTALL_OK');}finally{fs.rmSync(temp,{recursive:true,force:true});}})().catch(error=>{console.error(error);process.exit(1)});
""".replace('PACKAGE', json.dumps(package))
    if native:
        run([node, '-e', script])
    elif docker:
        mount = os.environ.get('ORE_DOCKER_WORKSPACE', str(ROOT))
        command = [docker, 'run', '--rm']
        if hasattr(os, 'getuid'):
            command += ['--user', f'{os.getuid()}:{os.getgid()}']
        command += ['-e', 'NPM_CONFIG_CACHE=/tmp/ore-npm-cache', '-v', f'{mount}:/repo:ro',
                    NODE_IMAGE, 'node', '-e', script]
        run(command)
    else:
        raise RuntimeError('Node/npm or Docker is required for the SDK clean-install check.')


def manifest_entry(path: Path) -> dict:
    return {'file': path.name, 'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-web-build', action='store_true',
                        help='Use an already built SDK and web/dist; still package and install-test them.')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12 or newer is required.')
    uv = shutil.which('uv')
    if not uv:
        parser.error('uv is required; install it before running the release build.')
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    if not args.skip_web_build:
        for directory in ['packages/sdk', 'web']:
            npm(directory, 'ci', '--no-audit', '--no-fund')
            npm(directory, 'run', 'build')
            npm(directory, 'test')
    for relative in ['web/dist/index.html', 'packages/sdk/dist/index.js', 'packages/sdk/dist/index.d.ts']:
        if not (ROOT / relative).is_file():
            raise RuntimeError(f'Missing build output: {relative}. Run without --skip-web-build.')
    sdk = json.loads((ROOT / 'packages/sdk/package.json').read_text())
    # Relative destination works identically in native npm and the Docker mount.
    npm('packages/sdk', 'pack', '--pack-destination', '../../dist')
    versions = {}
    for directory, package in [('.', 'ore_engine'), ('packages/ore-scholarly', 'ore_scholarly')]:
        metadata = tomllib.loads((ROOT / directory / 'pyproject.toml').read_text())
        versions[package] = metadata['project']['version']
        run([uv, 'build', '--out-dir', str(output), str(ROOT / directory)])
    engine_wheel = output / f'ore_engine-{versions["ore_engine"]}-py3-none-any.whl'
    scholarly_wheel = output / f'ore_scholarly-{versions["ore_scholarly"]}-py3-none-any.whl'
    artifacts = [engine_wheel, output / f'ore_engine-{versions["ore_engine"]}.tar.gz',
                 scholarly_wheel, output / f'ore_scholarly-{versions["ore_scholarly"]}.tar.gz',
                 output / f'ore-sdk-{sdk["version"]}.tgz']
    for path in artifacts:
        if not path.is_file():
            raise RuntimeError(f'Build did not produce expected artifact: {path}')
    with zipfile.ZipFile(engine_wheel) as archive:
        names = archive.namelist()
        assert 'ore/static/index.html' in names, 'Engine wheel is missing the console.'
        assert any(name.startswith('ore/static/assets/') and name.endswith('.js') for name in names)
    with tarfile.open(artifacts[1]) as archive:
        assert any(name.endswith('/web/dist/index.html') for name in archive.getnames())
    for path in artifacts[:4]:
        if path.suffix == '.whl':
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        else:
            with tarfile.open(path) as archive:
                names = archive.getnames()
        forbidden = {'auth.json', 'operator.token', 'secrets.enc', 'secret.key', '.env'}
        assert not any(set(Path(name).parts) & forbidden for name in names), f'Secret filename found in {path.name}'
    with tempfile.TemporaryDirectory(prefix='ore-release-smoke-') as temp:
        temporary = Path(temp)
        environment = temporary / 'venv'
        run([uv, 'venv', '--python', sys.executable, str(environment)])
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        ore = environment / ('Scripts/ore.exe' if os.name == 'nt' else 'bin/ore')
        run([uv, 'pip', 'install', '--python', str(python), str(engine_wheel), str(scholarly_wheel)])
        code = '''import json
from importlib.metadata import version
from importlib.resources import files
import ore, ore_scholarly
from ore.cli import app
from ore.server import create_app
root = files('ore') / 'static'
assert (root / 'index.html').is_file()
assert list((root / 'assets').iterdir())
assert ore_scholarly.list_sources()
assert ore_scholarly.list_runes()
print(json.dumps({'ore_engine': version('ore-engine'), 'ore_scholarly': version('ore-scholarly'), 'packaged_ui': True}))
'''
        smoke = json.loads(run([str(python), '-c', code], temporary, capture=True))
        run([str(ore), '--help'], temporary)
        # Rebuild the core wheel from its actual source distribution with no source checkout.
        rebuilt = temporary / 'rebuilt'
        run([uv, 'build', '--wheel', '--out-dir', str(rebuilt), str(artifacts[1])], temporary)
        with zipfile.ZipFile(next(rebuilt.glob('ore_engine-*.whl'))) as archive:
            assert 'ore/static/index.html' in archive.namelist()
    sdk_install_smoke(artifacts[4])
    entries = [manifest_entry(path) for path in artifacts]
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'published': False,
                'frontend': 'prebuilt' if args.skip_web_build else 'built_and_tested',
                'clean_install': smoke, 'cli_help': 'passed', 'sdist_rebuild': 'passed', 'sdk_clean_install': 'passed',
                'artifacts': entries}
    (output / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(f'{item["sha256"]}  {item["file"]}\n' for item in entries))
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
