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
    package = str(tarball) if native else '/repo/' + str(tarball.relative_to(ROOT))
    script = """const fs=require('node:fs');const os=require('node:os');const path=require('node:path');
const {execFileSync}=require('node:child_process');const {pathToFileURL}=require('node:url');
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'ore-sdk-install-'));
(async()=>{try{execFileSync('npm',['install','--prefix',temp,'--ignore-scripts','--no-audit','--no-fund',PACKAGE],{stdio:'inherit'});
const sdk=await import(pathToFileURL(path.join(temp,'node_modules/@ore/sdk/dist/index.js')).href);
if(typeof sdk.OreClient!=='function'||typeof sdk.parseEventStream!=='function')throw Error('SDK exports missing');
for(const name of ['createConversation','sendMessage','approvePlan','listConnections','listConversationFolders','branchConversation','interruptConversation','resumeConversation','conversationEvents'])if(typeof sdk.OreClient.prototype[name]!=='function')throw Error('SDK conversation method missing: '+name);
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
    parser.add_argument('--output', type=Path, default=ROOT / 'dist', help='Separate versioned output directory preserves previous release artifacts.')
    parser.add_argument('--skip-web-build', action='store_true',
                        help='Use an already built SDK and web/dist; still package and install-test them.')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12 or newer is required.')
    uv = shutil.which('uv')
    if not uv:
        parser.error('uv is required; install it before running the release build.')
    output = args.output.resolve()
    output.relative_to(ROOT)  # Docker packaging requires a path inside this checkout.
    output.mkdir(parents=True, exist_ok=True)
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
    npm('packages/sdk', 'pack', '--pack-destination', os.path.relpath(output, ROOT / 'packages/sdk'))
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
    companion_zip = output / f'ore-chrome-companion-{versions["ore_engine"]}.zip'
    with zipfile.ZipFile(companion_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / 'src/ore/companion_extension').iterdir()):
            if path.suffix in {'.js', '.json', '.html'}:
                archive.write(path, path.name)
    artifacts.append(companion_zip)
    for path in artifacts:
        if not path.is_file():
            raise RuntimeError(f'Build did not produce expected artifact: {path}')
    with zipfile.ZipFile(engine_wheel) as archive:
        names = archive.namelist()
        assert 'ore/static/index.html' in names, 'Engine wheel is missing the console.'
        for module in ('claude', 'connections', 'connection_agent', 'connection_intent', 'conversation_errors', 'provider_enrollment', 'provider_auth', 'credentials', 'source_wait', 'conversation_authority', 'conversation_contracts', 'conversation_library', 'conversation', 'conversation_api', 'public_stream', 'progress', 'workflow', 'workflow_adaptation', 'workflow_native', 'workflow_context', 'workflow_completion', 'workflow_recipes', 'agent_sessions', 'recipes', 'run_budget', 'planner_context', 'capabilities', 'sandbox', 'scheduler', 'pool', 'host_pool', 'challenge_policy', 'challenge_service'):
            assert f'ore/{module}.py' in names, f'Engine wheel is missing {module}'
        assert 'ore/companion_extension/manifest.json' in names, 'Engine wheel is missing the Chrome companion.'
        assert 'ore/companion_extension/background.js' in names
        for filename in ('Dockerfile', 'control.py', 'entrypoint.py', 'seccomp-chrome.json'):
            assert 'ore/desktop_assets/' + filename in names, 'Desktop runtime assets missing from engine wheel'
        assert any(name.startswith('ore/static/assets/') and name.endswith('.js') for name in names)
        assert any(name.startswith('ore/static/assets/') and name.endswith('.woff2') for name in names), 'Local fonts missing'
        for asset in ('brand/ore-original.svg','brand/ore-symbol.svg','fonts/inter-LICENSE.txt','fonts/noto-sans-kr-LICENSE.txt'):
            assert 'ore/static/'+asset in names, 'Brand or font license missing: '+asset
    with tarfile.open(artifacts[1]) as archive:
        assert any(name.endswith('/web/dist/index.html') for name in archive.getnames())
        for required in ('scripts/acceptance_workflow_runtime.py','scripts/benchmark_architecture.py','scripts/benchmark_fixtures.py','scripts/benchmark_architecture_v2.py','scripts/benchmark_fixtures_v2.py','scripts/benchmark_evaluation_v2.py','ORE_Original.svg','web/public/brand/ore-original.svg'):
            assert any(name.endswith('/'+required) for name in archive.getnames()), 'Source asset missing: '+required
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
        run([uv, 'pip', 'install', '--python', str(python), str(engine_wheel)])
        core_smoke = run([str(python), '-c', "from importlib.util import find_spec; from ore.engine import Engine; from ore.source_policy import source_catalog; from ore.evaluation import runtime_fingerprint; assert find_spec('ore_scholarly') is None; assert source_catalog()==[]; assert runtime_fingerprint()['digest']; print('CORE_ONLY_INSTALL_OK')"], temporary, capture=True)
        assert 'CORE_ONLY_INSTALL_OK' in core_smoke
        run([uv, 'pip', 'install', '--python', str(python), str(scholarly_wheel)])
        code = '''import json
from importlib.metadata import version
from importlib.resources import files
import ore, ore_scholarly
from ore.cli import app
from ore.server import create_app
root = files('ore') / 'static'
assert (root / 'index.html').is_file()
assert list((root / 'assets').iterdir())
assert (files('ore') / 'desktop_assets' / 'seccomp-chrome.json').is_file()
from ore.desktop import DesktopRuntime
assert DesktopRuntime.__name__ == 'DesktopRuntime'
assert ore_scholarly.list_sources()
assert ore_scholarly.list_runes()
# Scholarly browser contexts must survive wheel installation; a core-only runtime
# cannot verify EHJ archive access or supply its packaged frame/asset dependencies.
from copy import deepcopy
from ore.native_scope import journal_browser_context, native_scope_copy
mission = {'goal': 'Download all EHJ June 2024 articles', 'allowed_origins': [],
    'scope': {'journal_id': 'ehj', 'article_types': 'all'},
    'publication_window': {'from': '2024-06-01', 'until_exclusive': '2024-07-01'},
    'artifact_roles': ['main_pdf', 'supplement'], 'completeness': 'systematic'}
profile = {'id': 'public', 'principal_id': 'operator'}
original = deepcopy((mission, profile))
for path in ('/eurheartj/issue-archive', '/eurheartj/issue-archive/2024'):
    checkpoint = 'https://academic.oup.com' + path
    context = journal_browser_context(checkpoint)
    assert context['protocol_id'] == 'journal.ehj' and context['digest']
    assert context['success_text'] == ['European Heart Journal', 'Oxford Academic']
    assert 'https://challenges.cloudflare.com' in context['support_origins']
    assert context['asset_origins'] == ['https://oup.silverchair-cdn.com', 'https://watermark02.silverchair.com']
    copied, access = native_scope_copy(mission, profile, checkpoint)
    assert copied['desktop_success_text'] == context['success_text']
    assert copied['allowed_origins'] == ['https://academic.oup.com', 'https://challenges.cloudflare.com',
                                         'https://oup.silverchair-cdn.com', 'https://watermark02.silverchair.com']
    for field in ('scope', 'publication_window', 'artifact_roles', 'completeness'):
        assert copied[field] == mission[field], 'Packaged browser context changed collection criteria'
    assert access['id'] == profile['id'] and access['principal_id'] == profile['principal_id']
assert (mission, profile) == original
from ore.capabilities import CapabilityRegistry
from ore.conversation import ConversationManager
from ore.workflow import WorkflowManager
print(json.dumps({'ore_engine': version('ore-engine'), 'ore_scholarly': version('ore-scholarly'), 'packaged_ui': True, 'scholarly_browser_context': True}))
'''
        smoke = json.loads(run([str(python), '-c', code], temporary, capture=True))
        run([str(ore), '--help'], temporary)
        run([uv, 'pip', 'install', '--python', str(python), str(engine_wheel) + '[claude]'])
        run([str(python), '-c', "from ore.claude import sdk_module, ClaudeBackend; sdk=sdk_module(); assert hasattr(sdk, 'ClaudeSDKClient'); print('CLAUDE_OPTIONAL_INSTALL_OK')"], temporary)
        # Rebuild the core wheel from its actual source distribution with no source checkout.
        rebuilt = temporary / 'rebuilt'
        run([uv, 'build', '--wheel', '--out-dir', str(rebuilt), str(artifacts[1])], temporary)
        with zipfile.ZipFile(next(rebuilt.glob('ore_engine-*.whl'))) as archive:
            assert 'ore/static/index.html' in archive.namelist()
            assert 'ore/desktop_assets/seccomp-chrome.json' in archive.namelist()
    sdk_install_smoke(artifacts[4])
    entries = [manifest_entry(path) for path in artifacts]
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'published': False,
                'frontend': 'prebuilt' if args.skip_web_build else 'built_and_tested',
                'clean_install': smoke, 'core_only_install': 'passed', 'claude_optional_install': 'passed', 'cli_help': 'passed', 'sdist_rebuild': 'passed', 'sdk_clean_install': 'passed',
                'artifacts': entries}
    (output / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(f'{item["sha256"]}  {item["file"]}\n' for item in entries))
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
