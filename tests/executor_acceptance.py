"""Opt-in two-container executor acceptance. No model calls or real account access."""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid


def main():
    suffix = uuid.uuid4().hex[:8]
    network = 'ore-execution-test-' + suffix
    coordinator = network + '-coordinator'
    workers = [network + '-a', network + '-b']
    validation_network = network + '-validation'
    state_volume = network + '-state'
    validator = network + '-validator'
    names = [coordinator, *workers, validator]
    environment = {**os.environ, 'ORE_EXECUTOR_ENROLLMENT_TOKEN': secrets.token_urlsafe(40), 'ORE_VALIDATOR_TOKEN': secrets.token_urlsafe(40), 'ORE_VALIDATOR_URL': 'http://validator:8770'}
    enforce_limits = os.environ.get('ORE_EXECUTOR_TEST_RESOURCE_LIMITS', 'true').lower() != 'false'
    validator_limits = ['--cpus', '2', '--memory', '2g', '--memory-swap', '2g', '--pids-limit', '128'] if enforce_limits else []
    executor_limits = ['--cpus', '2', '--memory', '4g', '--memory-swap', '4g', '--pids-limit', '512'] if enforce_limits else []
    report = {'passed': False, 'containers': names}
    def docker(*args, check=True):
        result = subprocess.run(['docker', *args], env=environment, check=check, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return (result.stdout + (result.stderr if args and args[0] == 'logs' else '')).strip()
    try:
        docker('network', 'create', network)
        docker('volume', 'create', state_volume)
        docker('network', 'create', '--internal', validation_network)
        docker('run', '-d', '--name', validator, '--network', validation_network, '--network-alias', 'validator',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            *validator_limits,
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=1g',
            '-e', 'ORE_VALIDATOR_TOKEN', '--entrypoint', 'python3', 'ore-executor:test', '-m', 'ore.validation')
        common = ['--network', network, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=2g', '--shm-size', '1g',
            *executor_limits, '-e', 'ORE_EXECUTOR_ENROLLMENT_TOKEN', '-e', 'ORE_VALIDATOR_TOKEN', '-e', 'ORE_VALIDATOR_URL']
        docker('run', '-d', '--name', coordinator, '--network-alias', 'coordinator', '--network-alias', 'coordinator.ore.test', *common,
            '-e', 'ORE_FIXTURE_STATE_DIR=/var/lib/ore', '-v', state_volume + ':/var/lib/ore',
            '--entrypoint', 'python3', 'ore-executor:test', '-m', 'uvicorn', 'executor_fixture_server:app', '--host', '0.0.0.0', '--port', '8765')
        docker('network', 'connect', validation_network, coordinator)
        for _ in range(45):
            try:
                text = docker('exec', coordinator, 'python3', '-c', "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=2).read().decode())")
                if json.loads(text)['ok']:
                    break
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                time.sleep(1)
        else:
            raise RuntimeError('Fixture coordinator did not become healthy')
        for name in workers:
            docker('run', '-d', '--name', name, *common, 'ore-executor:test', '--server', 'http://coordinator:8765')
            docker('network', 'connect', validation_network, name)
        for _ in range(30):
            text = docker('exec', coordinator, 'python3', '-c', "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=2).read().decode())")
            if json.loads(text)['available_executors'] == 2:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Two real executor containers did not register')
        code = "import urllib.request; req=urllib.request.Request('http://127.0.0.1:8765/test/parallel',data=b'',method='POST');print(urllib.request.urlopen(req,timeout=240).read().decode())"
        report = json.loads(docker('exec', coordinator, 'python3', '-c', code))
        docker('restart', coordinator)
        for _ in range(45):
            try:
                docker('exec', coordinator, 'python3', '-c', "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=2).read()")
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        code = "import urllib.request;req=urllib.request.Request('http://127.0.0.1:8765/test/recover',data=b'',method='POST');print(urllib.request.urlopen(req,timeout=120).read().decode())"
        report = json.loads(docker('exec', coordinator, 'python3', '-c', code))
        code = "import urllib.request;req=urllib.request.Request('http://127.0.0.1:8765/test/prepare-recreate',data=b'',method='POST');print(urllib.request.urlopen(req,timeout=60).read().decode())"
        lost = json.loads(docker('exec', coordinator, 'python3', '-c', code))
        restart_name = next(name for name in workers if 'executor-' + json.loads(docker('inspect', name))[0]['Config']['Hostname'] == lost['worker_id'])
        docker('restart', restart_name)
        code = "import urllib.request;req=urllib.request.Request('http://127.0.0.1:8765/test/recreate',data=b'',method='POST');print(urllib.request.urlopen(req,timeout=180).read().decode())"
        report = json.loads(docker('exec', coordinator, 'python3', '-c', code))
        inspections = []
        for name in workers:
            row = json.loads(docker('inspect', name))[0]
            env_names = [entry.split('=',1)[0] for entry in row['Config']['Env']]
            inspections.append({'id': row['Id'], 'image_id': row['Image'], 'name': name, 'user': row['Config']['User'],
                'read_only': row['HostConfig']['ReadonlyRootfs'],
                'memory_limit': row['HostConfig']['Memory'], 'pids_limit': row['HostConfig']['PidsLimit'],
                'mount_destinations': [m['Destination'] for m in row.get('Mounts',[])],
                'forbidden_env_names': sorted(set(env_names) & {'ORE_AUTH_TOKEN','ORE_DATABASE_URL','ORE_SECRET_KEY','OPENAI_API_KEY'})})
        validator_row = json.loads(docker('inspect', validator))[0]
        validator_names = [entry.split('=',1)[0] for entry in validator_row['Config']['Env']]
        report['validator'] = {'id': validator_row['Id'], 'image_id': validator_row['Image'], 'network_internal': True,
            'memory_limit': validator_row['HostConfig']['Memory'], 'pids_limit': validator_row['HostConfig']['PidsLimit'],
            'tmpfs': validator_row['HostConfig']['Tmpfs'],
            'only_private_network': set(validator_row['NetworkSettings']['Networks']) == {validation_network},
            'mount_destinations': [m['Destination'] for m in validator_row.get('Mounts',[])],
            'forbidden_env_names': sorted(set(validator_names) & {'ORE_AUTH_TOKEN','ORE_DATABASE_URL','ORE_SECRET_KEY','OPENAI_API_KEY','ORE_EXECUTOR_ENROLLMENT_TOKEN'})}
        parser_probe = "import hashlib,json,pathlib,ore.validation as v;print(json.dumps({'module_sha256':hashlib.sha256(pathlib.Path(v.__file__).read_bytes()).hexdigest(),'per_document_subprocess':hasattr(v,'run_parser'),'deadline_seconds':v.PARSER_TIMEOUT_SECONDS,'max_response_bytes':v.MAX_RESPONSE}))"
        report['validator']['implementation'] = json.loads(docker('exec', validator, 'python3', '-c', parser_probe))
        assert report['validator']['only_private_network'] and not report['validator']['forbidden_env_names']
        if enforce_limits:
            assert report['validator']['memory_limit'] == 2 * 1024**3 and report['validator']['pids_limit'] == 128
        assert 'noexec' in report['validator']['tmpfs']['/tmp']
        report['container_inspection'] = inspections
        assert len({item['id'] for item in inspections}) == 2
        assert all(not row['forbidden_env_names'] for row in inspections)
        assert report['coordinator_browser_started'] is False
    except Exception as exc:
        report['error'] = type(exc).__name__
        for name in names:
            logs = docker('logs', '--tail', '70', name, check=False)
            if logs:
                print(json.dumps({'container':name,'logs':logs}))
        raise
    finally:
        report['resource_limits_requested'] = enforce_limits
        report['resource_limit_validation'] = 'enforced' if enforce_limits and report.get('passed') else 'not_validated'
        if not enforce_limits:
            report['limitation'] = 'Fixture explicitly omitted cgroup CPU/memory/PID limits; deployment resource ceilings are not validated by this run.'
        destination = Path('.ore/reports/executor-container-acceptance.json')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + '\n')
        destination.with_name('executor-container-acceptance-' + suffix + '.json').write_text(json.dumps(report, indent=2) + '\n')
        for name in reversed(names):
            docker('rm', '-f', name, check=False)
        docker('network', 'rm', network, check=False)
        docker('network', 'rm', validation_network, check=False)
        docker('volume', 'rm', state_volume, check=False)
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
