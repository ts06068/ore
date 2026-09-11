"""Trusted host-side Docker provisioning. No Docker socket enters executors.

Run this process on the Docker host with a dedicated ORE_POOL_TOKEN. Image,
network and resource settings are operator startup configuration, never model
arguments. The diagnostic rlimit mode must be explicitly selected; it does not
provide a hard container-wide memory/CPU ceiling and is not a production fallback.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class HostPoolConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    broker_id: str = 'host'
    server: str
    image: str = 'ore-executor:0.4.0rc1'
    network: str = 'ore-execution'
    validator_network: str = 'ore-validation'
    hard_cap: int = Field(default=5, ge=1, le=64)
    cpus: float = Field(default=2, ge=.25, le=16)
    memory_bytes: int = Field(default=2 * 1024**3, ge=256 * 1024**2, le=32 * 1024**3)
    pids: int = Field(default=256, ge=64, le=2048)
    tmp_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2, le=16 * 1024**3)
    resource_mode: str = 'cgroup'
    validator_url: str | None = None
    state_dir: Path = Path('.ore/host-pool')

    @field_validator('broker_id', 'network', 'validator_network')
    @classmethod
    def safe_name(cls, value):
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', value) or value in ('host', 'none'):
            # A broker called host is harmless; network host is prohibited below.
            if value != 'host': raise ValueError('Unsafe name')
        return value

    @field_validator('server', 'validator_url')
    @classmethod
    def safe_url(cls, value):
        if value is None: return value
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Expected an HTTP(S) service URL without credentials/query')
        return value.rstrip('/')

    @field_validator('image')
    @classmethod
    def fixed_image(cls, value):
        if len(value) > 512 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]*', value):
            raise ValueError('Expected a fixed Docker image name or digest')
        return value

    @field_validator('resource_mode')
    @classmethod
    def explicit_mode(cls, value):
        if value not in ('cgroup', 'diagnostic_rlimit'): raise ValueError('Unknown resource mode')
        return value


class HostPoolError(RuntimeError):
    pass


class DockerObjectMissing(HostPoolError):
    pass


class DockerHostPool:
    def __init__(self, config: HostPoolConfig, *, token=None, validator_token=None, command=None):
        if config.network in ('host', 'none') or config.validator_network in ('host', 'none'): raise ValueError('Use a dedicated bridge network')
        self.config = config
        self.token = token or os.environ.get('ORE_POOL_TOKEN')
        self.validator_token = validator_token or os.environ.get('ORE_VALIDATOR_TOKEN')
        if not self.token: raise ValueError('ORE_POOL_TOKEN is required')
        if config.validator_url and not self.validator_token: raise ValueError('Validator role token is required')
        if config.resource_mode == 'cgroup' and not config.validator_url:
            raise ValueError('Production pool requires a credential-isolated validator service')
        self.command = command or self._command
        self.image_id = None
        self.cooldown_until = 0
        self.last_error = None
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = config.state_dir / (config.broker_id + '.json')
        self.containers = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    async def _command(self, *args):
        process = await asyncio.create_subprocess_exec('docker', *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try: out, err = await asyncio.wait_for(process.communicate(), 45)
        except BaseException:
            process.kill(); await process.communicate(); raise
        if process.returncode:
            # No command/environment dump; Docker errors may contain host paths.
            if args[0] == 'inspect' and (b'no such object:' in err.lower() or b'no such container:' in err.lower()):
                raise DockerObjectMissing('container_absent')
            raise HostPoolError('docker_' + str(args[0]) + '_failed: ' + err.decode(errors='replace')[:700])
        return out.decode()

    def _save(self):
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.containers)); tmp.chmod(0o600); tmp.replace(self.state_path)

    async def inventory(self):
        result = []
        for worker, item in list(self.containers.items()):
            try:
                value = json.loads(await self.command('inspect', '--format', '{{json .}}', item['name']))
                labels = value.get('Config', {}).get('Labels') or {}
                if labels.get('ore.pool.broker') != self.config.broker_id or labels.get('ore.pool.worker') != worker:
                    raise ValueError('Container ownership changed; refusing all management')
                state = value.get('State', {}).get('Status', 'unknown')
            except DockerObjectMissing:
                state = 'absent'
            except HostPoolError:
                state = 'unknown'
            result.append({'worker_id': worker, 'container_id': item['id'], 'state': state})
        return result

    async def discover(self):
        info = json.loads(await self.command('info', '--format', '{{json .}}'))
        total_memory, cpus = int(info.get('MemTotal', 0)), int(info.get('NCPU', 0))
        if total_memory <= 0 or cpus <= 0: raise HostPoolError('resource_capacity_unavailable')
        # Daemon-wide usage, not just this pool. Docker stats reports each running
        # container; a reserve covers host services and observation uncertainty.
        used, cpu = 0, 0.0
        stats = await self.command('stats', '--no-stream', '--format', '{{json .}}')
        units = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'kB': 1000, 'MB': 1000**2, 'GB': 1000**3}
        for line in stats.splitlines():
            row = json.loads(line)
            match = re.match(r'([0-9.]+)\s*([A-Za-z]+)', row.get('MemUsage', ''))
            if match: used += float(match[1]) * units.get(match[2], 1)
            cpu += float(row.get('CPUPerc', '0%').rstrip('%')) / 100
        inventory = await self.inventory()
        active = sum(r['state'] not in ('absent', 'exited', 'dead') for r in inventory)
        spare_memory = max(0, total_memory * .8 - used)
        spare_cpu = max(0, cpus * .8 - cpu)
        additional = min(int(spare_memory // self.config.memory_bytes), int(spare_cpu // self.config.cpus))
        capacity = min(self.config.hard_cap, int(total_memory * .8 // self.config.memory_bytes),
                       int(cpus * .8 // self.config.cpus), active + max(0, additional))
        return capacity, {'memory_ratio': min(1, used / total_memory), 'cpu_ratio': min(1, cpu / cpus),
                          'available_memory_bytes': int(spare_memory), 'logical_cpus': cpus}

    def launch_arguments(self, grant, env_path):
        c = self.config
        name = 'ore-pool-' + grant['worker_id']
        args = ['run', '-d', '--name', name, '--label', 'ore.pool.broker=' + c.broker_id,
                '--label', 'ore.pool.worker=' + grant['worker_id'], '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--user=10001:10001', '--pids-limit=' + str(c.pids),
                '--network=' + c.network, '--tmpfs=/tmp:rw,nosuid,nodev,size=' + str(c.tmp_bytes),
                '--shm-size=268435456', '--env-file', str(env_path), '--init']
        if c.validator_url and c.validator_network != c.network:
            args += ['--network=' + c.validator_network]
        if c.resource_mode == 'cgroup':
            args += ['--memory=' + str(c.memory_bytes), '--memory-swap=' + str(c.memory_bytes), '--cpus=' + str(c.cpus)]
        else:
            # Explicit diagnostic only. Per-process limits do not equal aggregate
            # cgroup limits; preserve PID hard cap and make this visible in reports.
            args += ['--cgroup-parent=/', '--ulimit=nofile=1024:1024', '--ulimit=cpu=120:120']
        args += ['--entrypoint=python3', self.image_id or c.image]
        if c.resource_mode == 'diagnostic_rlimit':
            wrapper = 'import resource,sys; limit=int(sys.argv.pop(1)); resource.setrlimit(resource.RLIMIT_AS,(limit,limit)); from ore.executor import main; main()'
            args += ['-c', wrapper, str(c.memory_bytes), '--server', c.server]
        else:
            args += ['-m', 'ore.executor', '--server', c.server]
        return args

    async def launch(self, grant):
        worker = grant['worker_id']
        if not re.fullmatch(r'pool-[A-Za-z0-9_-]+', worker): raise HostPoolError('invalid_worker_identity')
        if worker in self.containers: return
        occupied = sum(r['state'] not in ('absent', 'exited', 'dead') for r in await self.inventory())
        if occupied >= self.config.hard_cap: raise HostPoolError('operator_pool_capacity_exhausted')
        if not self.image_id:
            self.image_id = (await self.command('image', 'inspect', '--format', '{{.Id}}', self.config.image)).strip()
            if not re.fullmatch(r'sha256:[a-f0-9]{64}', self.image_id): raise HostPoolError('image_digest_unavailable')
        env = {'ORE_EXECUTOR_ID': worker, 'ORE_EXECUTOR_ENROLLMENT_TOKEN': grant['enrollment_token'],
               'ORE_EXECUTOR_NETWORK_ZONE': grant['network_zone'], 'ORE_CONTAINER_EXECUTOR': '1',
               'ORE_EXECUTOR_STATE_DIR': '/tmp/ore-executor', 'ORE_STATE_DIR': '/tmp/ore-executor'}
        if self.config.validator_url:
            env.update(ORE_VALIDATOR_URL=self.config.validator_url, ORE_VALIDATOR_TOKEN=self.validator_token)
        if any('\n' in str(v) or '\r' in str(v) for v in env.values()): raise HostPoolError('invalid_environment_value')
        descriptor, path = tempfile.mkstemp(prefix='ore-pool-env-', dir=self.config.state_dir)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(''.join(k + '=' + str(v) + '\n' for k, v in env.items()))
            ident = (await self.command(*self.launch_arguments(grant, Path(path)))).strip()
        finally: Path(path).unlink(missing_ok=True)
        self.containers[worker] = {'id': ident, 'name': 'ore-pool-' + worker, 'started_at': time.time()}
        self._save()

    async def drain(self, worker):
        item = self.containers.get(worker)
        if not item: return
        # Coordinator has already fenced admission by setting state=draining.
        rows = await self.inventory()
        observed = next(r for r in rows if r['worker_id'] == worker)['state']
        if observed == 'unknown': raise HostPoolError('container_state_unconfirmed')
        if observed not in ('absent', 'exited', 'dead'):
            await self.command('stop', '--time', '10', item['name'])
        try: await self.command('rm', item['name'])
        except HostPoolError: pass
        self.containers[worker]['stopped'] = True; self._save()

    async def tick(self, client):
        capacity, resources = await self.discover()
        inventory = await self.inventory()
        response = await client.post('/v1/pool/report', json={'broker_id': self.config.broker_id, 'capacity': capacity,
            'resources': resources, 'containers': inventory, 'resource_mode': self.config.resource_mode})
        response.raise_for_status(); actions = response.json()
        for item in inventory:
            if item['state'] == 'absent' and self.containers.get(item['worker_id'], {}).get('stopped'):
                self.containers.pop(item['worker_id'], None)
        self._save()
        for action in actions['drain']: await self.drain(action['worker_id'])
        for item in inventory:
            if item['state'] in ('exited', 'dead') and item['worker_id'] in self.containers:
                await self.drain(item['worker_id'])
        if time.time() >= self.cooldown_until:
            for grant in actions['launch']:
                try: await self.launch(grant)
                except HostPoolError as exc:
                    self.last_error = str(exc); self.cooldown_until = time.time() + 45; break
        return {'desired': actions['desired'], 'ready': actions['ready'], 'capacity': capacity,
                'resource_mode': self.config.resource_mode, 'error': self.last_error,
                'containers': len(self.containers)}

    async def run(self, *, once=False):
        async with httpx.AsyncClient(base_url=self.config.server, timeout=30,
            headers={'authorization': 'Bearer ' + self.token}) as client:
            while True:
                result = await self.tick(client)
                print(json.dumps(result), flush=True)
                if once: return result
                await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--broker-id', default='host')
    parser.add_argument('--image', default='ore-executor:0.4.0rc1')
    parser.add_argument('--network', default='ore-execution')
    parser.add_argument('--validator-network', default='ore-validation')
    parser.add_argument('--hard-cap', type=int, default=5)
    parser.add_argument('--cpus', type=float, default=2)
    parser.add_argument('--memory-bytes', type=int, default=2 * 1024**3)
    parser.add_argument('--resource-mode', choices=['cgroup', 'diagnostic_rlimit'], default='cgroup')
    parser.add_argument('--validator-url', default=os.environ.get('ORE_VALIDATOR_URL'))
    parser.add_argument('--state-dir', type=Path, default=Path('.ore/host-pool'))
    parser.add_argument('--once', action='store_true')
    args = vars(parser.parse_args()); once = args.pop('once')
    asyncio.run(DockerHostPool(HostPoolConfig(**args)).run(once=once))


if __name__ == '__main__': main()
