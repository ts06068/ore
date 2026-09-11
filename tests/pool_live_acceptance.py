"""Manual real-container acceptance: no models, publishers or existing account state.

This environment's shell cannot reach the Docker host bridge. The fixture client
uses docker exec solely as an HTTP transport into its private coordinator; the
production host broker uses its ordinary HTTP client. No worker mounts a socket.
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

import httpx
from ore.host_pool import DockerHostPool, HostPoolConfig


async def docker(*args, input_bytes=None):
    process = await asyncio.create_subprocess_exec('docker', *args, stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(process.communicate(input_bytes), 50)
    if process.returncode: raise RuntimeError('docker_' + args[0] + '_failed: ' + err.decode()[-400:])
    return out.decode()


class FixtureHTTPClient:
    def __init__(self, name): self.name = name
    async def post(self, path, json=None):
        code = "import json,sys,os,urllib.request,urllib.error; value=json.load(sys.stdin); req=urllib.request.Request('http://127.0.0.1:8765'+value['path'],data=json.dumps(value['body']).encode(),headers={'content-type':'application/json','authorization':'Bearer '+os.environ['ORE_POOL_TOKEN']}); response=urllib.request.urlopen(req,timeout=40); print(response.read().decode())"
        content = await docker('exec', '-i', self.name, 'python3', '-c', code,
            input_bytes=__import__('json').dumps({'path': path, 'body': json or {}}).encode())
        return httpx.Response(200, json=__import__('json').loads(content), request=httpx.Request('POST', 'http://fixture' + path))


async def main():
    stamp = str(int(time.time())); base = Path('.ore/pool-live-' + stamp).resolve(); base.mkdir(mode=0o700)
    network, coordinator = 'ore-pool-fixture-' + stamp, 'ore-pool-coordinator-' + stamp
    token = secrets.token_urlsafe(32)
    report = {'schema_version': 'ore.host-pool-acceptance/v1', 'resource_mode': 'diagnostic_rlimit',
        'limitation': 'Per-process address-space/CPU-time limits and PID cap; no hard aggregate cgroup memory/CPU ceiling. HTTP fixture only; native desktop and validator parsing not exercised.',
        'external_model_calls': 0, 'publisher_requests': 0, 'checks': {}, 'state_dir': str(base)}
    pool = DockerHostPool(HostPoolConfig(server='http://pool-coordinator:8765', broker_id='fixture' + stamp, network=network,
        image='ore-executor:pool-test', hard_cap=5, cpus=.25, memory_bytes=2 * 1024**3,
        resource_mode='diagnostic_rlimit', state_dir=base / 'host'), token=token)
    started = time.monotonic()
    try:
        await docker('network', 'create', network)
        fd, env_path = tempfile.mkstemp(prefix='fixture-env-', dir=base)
        try:
            with os.fdopen(fd, 'w') as stream: stream.write('ORE_POOL_TOKEN=' + token + '\n')
            await docker('run', '-d', '--name', coordinator, '--network', network, '--network-alias', 'pool-coordinator',
                '--read-only', '--user=10001:10001', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=256',
                '--cgroup-parent=/', '--ulimit=cpu=120:120', '--tmpfs=/tmp:rw,nosuid,nodev,size=536870912',
                '--env-file', env_path, '--entrypoint=python3', 'ore-executor:pool-test',
                '-m', 'uvicorn', 'pool_fixture_server:app', '--host', '0.0.0.0', '--port', '8765', '--log-level', 'error')
        finally: Path(env_path).unlink(missing_ok=True)
        client = FixtureHTTPClient(coordinator)
        for _ in range(40):
            try: before = (await client.post('/test/prepare')).json(); break
            except RuntimeError: await asyncio.sleep(.2)
        else: raise RuntimeError('fixture_coordinator_not_ready')
        report['checks']['initial_desired_five_ready_zero'] = before['desired_executors'] == 5 and before['ready_executors'] == 0
        report['launch'] = await pool.tick(client)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status = (await client.post('/test/state')).json()
            if status['ready'] == 5: break
            await asyncio.sleep(.4)
        report['checks']['five_registered_executors'] = status['ready'] == 5
        if status['ready'] != 5: raise RuntimeError('five_executors_not_registered')
        result = (await client.post('/test/run')).json(); report['results'] = result
        report['checks']['five_distinct_containers'] = len(set(result['worker_ids'])) == 5
        report['checks']['actual_http_results_match'] = len(result['results']) == 5 and all(r['status'] == 200 and r['exact_content'] for r in result['results'])
        inspected = []
        for worker, item in pool.containers.items():
            data = json.loads(await docker('inspect', item['name']))[0]
            names = {x.split('=', 1)[0] for x in data['Config']['Env']}
            inspected.append({'worker_id': worker, 'image': data['Image'], 'user': data['Config']['User'],
                'mounts': [m['Destination'] for m in data['Mounts']], 'privileged': data['HostConfig']['Privileged'],
                'forbidden_credential_names_present': sorted(names & {'ORE_POOL_TOKEN', 'ORE_AUTH_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CODEX_HOME'})})
        report['container_isolation'] = inspected
        report['checks']['no_host_auth_or_socket_mounts'] = all(not x['forbidden_credential_names_present'] and not x['privileged'] and not any('.codex' in m or 'docker.sock' in m for m in x['mounts']) for x in inspected)
        await pool.tick(client); await pool.tick(client)
        report['checks']['idle_containers_drained'] = not pool.containers
        report['status'] = 'passed' if all(report['checks'].values()) else 'failed'
    except Exception as exc:
        report['status'] = 'failed'; report['error'] = type(exc).__name__ + ':' + str(exc)[:400]
    finally:
        for item in list(pool.containers.values()):
            try: await docker('rm', '-f', item['name'])
            except Exception: pass
        try: await docker('rm', '-f', coordinator)
        except Exception: pass
        try: await docker('network', 'rm', network)
        except Exception: pass
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        target = Path('.ore/reports/host-pool-live.json'); target.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'status': report['status'], 'checks': report['checks'], 'report': str(target), 'error': report.get('error')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(asyncio.run(main()))
