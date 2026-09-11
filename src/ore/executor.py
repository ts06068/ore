"""Isolated execution process. Connects outbound; does not run Codex or a shell agent."""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
from copy import deepcopy
import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from types import SimpleNamespace

import httpx

from .browser import BrowserManager
from .execution import EXECUTOR_TOOLS
from .policy import AccessDenied, AccessPolicy, GuardedTransport, RateLimiter
from .store import ControlConflict, LeaseLost
from .tools import ToolRuntime
from .vault import Vault


def pack(value):
    if isinstance(value, bytes):
        return {'__ore_bytes__': base64.b64encode(value).decode()}
    if isinstance(value, dict):
        return {key: pack(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [pack(item) for item in value]
    return value


def source_probe_policy(profile, source, operation):
    """Recheck one explicitly requested operation without erasing saved evidence."""
    from .source_policy import canonical_operation, canonical_source, OPERATIONS
    source, operation = canonical_source(source), canonical_operation(operation)
    if operation not in ('search', 'resolve'):
        raise AccessDenied('Source check supports search or resolve only')
    probe_profile = deepcopy(profile)
    probe_profile.get('source_readiness', {}).get(source, {}).pop(operation, None)
    allow = {name: [source] if name == operation else [] for name in OPERATIONS}
    mission = {'sources': [source], 'source_policy': {'allow': allow}}
    return AccessPolicy(mission, probe_profile, operation=operation, source_hint=source)


class ReceiptOutbox:
    """Private, boot-bound receipts survive reconnects without accepting stale results."""
    def __init__(self, base_dir, worker_id, boot_id):
        owner = hashlib.sha256(worker_id.encode()).hexdigest()
        self.path = Path(base_dir) / 'receipts' / owner / boot_id
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.chmod(0o700)

    def _save(self, path, value):
        temporary = self.path / (uuid.uuid4().hex + '.tmp')
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def enqueue(self, report):
        envelope = {key: report[key] for key in ('assignment_id', 'capability', 'command_id')}
        name = hashlib.sha256(str(envelope['command_id']).encode()).hexdigest()
        self._save(self.path / (name + '.json'), {'phase': 'result', 'envelope': envelope, 'report': pack(report)})

    async def flush(self, client, prefix):
        """Keep records on transient failures; remove only a checked acknowledgement."""
        complete = True
        for path in sorted(self.path.glob('*.json')):
            value = json.loads(path.read_text())
            try:
                if value['phase'] == 'result':
                    response = await client.post(prefix + '/result', json=value['report'])
                    if response.status_code == 409:
                        value = {'phase': 'cancelled', 'envelope': value['envelope']}
                        self._save(path, value)
                    else:
                        response.raise_for_status()
                        if response.json().get('status') not in ('recorded', 'duplicate'):
                            raise ValueError('Coordinator did not acknowledge the executor result')
                        path.unlink()
                        continue
                response = await client.post(prefix + '/cancelled', json=value['envelope'])
                response.raise_for_status()
                payload = response.json()
                if payload.get('status') not in ('acknowledged', 'duplicate', 'settled'):
                    raise ValueError('Coordinator did not acknowledge cancellation')
                if payload.get('command_id') not in (None, value['envelope']['command_id']):
                    raise ValueError('Cancellation acknowledgement names another command')
                path.unlink()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    raise AccessDenied('Executor receipt capability was revoked; pending receipt is retained') from None
                complete = False
            except (httpx.HTTPError, OSError, ValueError):
                complete = False
        return complete


class ExecutorHeartbeat:
    """A separate control thread remains responsive during synchronous store RPCs."""
    def __init__(self, server, token, prefix, on_cancel, *, interval=5, client=None):
        self.stop_event = threading.Event()
        self.client = client or httpx.Client(base_url=server, headers={'authorization': 'Bearer ' + token}, timeout=5)
        self.prefix, self.on_cancel, self.interval = prefix, on_cancel, interval
        self.thread = threading.Thread(target=self.run, name='ore-executor-heartbeat', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        try:
            while not self.stop_event.wait(self.interval):
                try:
                    response = self.client.post(self.prefix + '/heartbeat', json={})
                    response.raise_for_status()
                    self.on_cancel(response.json().get('cancel_commands', []))
                except (httpx.HTTPError, OSError, ValueError):
                    # Loss of coordinator contact revokes permission to keep acting.
                    self.on_cancel(None)
        finally:
            self.client.close()

    async def close(self):
        self.stop_event.set()
        await asyncio.to_thread(self.thread.join, 6)
        if self.thread.is_alive():
            raise RuntimeError('Executor heartbeat has not stopped')


class ScopedStore:
    def __init__(self, context):
        self.context = context

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.context.rpc(name, *args, **kwargs)

    def artifacts(self, *args, **kwargs):
        rows = self.context.rpc('artifacts', *args, **kwargs)
        return [{**row, 'path': str(self.context.local_artifacts[row['id']])} if row['id'] in self.context.local_artifacts else row for row in rows]

    def add_artifact(self, job_id, record, **kwargs):
        context = self.context
        with context.io_operation():
            path = Path(record['path']).resolve()
            if not path.is_relative_to(context.state_dir):
                raise AccessDenied('Executor upload path is outside its private state')
            data = context.envelope() | {'record': {key: value for key, value in record.items() if key != 'path'}}
            response = context.client.post(context.prefix + '/uploads', json=pack(data))
            context.check(response)
            context.check_cancelled()
            ident = response.json()['upload_id']
            def chunks():
                with path.open('rb') as stream:
                    while True:
                        context.check_cancelled()
                        chunk = stream.read(64 * 1024)
                        if not chunk:
                            return
                        yield chunk
            response = context.client.put(context.prefix + '/uploads/' + ident, content=chunks(),
                headers={'x-ore-assignment': context.assignment['capability'], 'content-length': str(path.stat().st_size)}, timeout=5)
            context.check(response)
            context.check_cancelled()
            artifact = response.json()
            context.local_artifacts[artifact['id']] = path
            return artifact


class ScopedSecrets:
    def __init__(self, context):
        self.context = context

    def get(self, ref):
        return self.context.rpc('secret.get', ref)

    def set(self, ref, value):
        return self.context.rpc('secret.set', ref, value)

    def merge_browser_profile(self, ref, baseline, current):
        return self.context.rpc('secret.merge_browser_profile', ref, baseline, current)


class ScopedCoverage:
    def __init__(self, context):
        self.context = context

    def capture_snapshot(self, *args, **kwargs):
        return self.context.rpc('coverage.capture_snapshot', *args, **kwargs)

    def check_download(self, *args, **kwargs):
        return self.context.rpc('coverage.check_download', *args, **kwargs)

    def bind_artifact(self, *args, **kwargs):
        return self.context.rpc('coverage.bind_artifact', *args, **kwargs)

    def record_attempt(self, *args, **kwargs):
        return self.context.rpc('coverage.record_attempt', *args, **kwargs)

    def register_candidates(self, *args, **kwargs):
        return self.context.rpc('coverage.register_candidates', *args, **kwargs)


class ExecutionContext:
    def __init__(self, server, worker_id, token, assignment, base_dir):
        self.assignment = assignment
        self.command_id = None
        self.cancel_event = threading.Event()
        self._io_lock = threading.Lock()
        self._io_active = 0
        self.prefix = f'/v1/execution/workers/{worker_id}'
        self.client = httpx.Client(base_url=server, headers={'authorization': 'Bearer ' + token}, timeout=5)
        self.state_dir = (base_dir / assignment['id']).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ('staging', 'vault'):
            (self.state_dir / name).mkdir(mode=0o700, exist_ok=True)
        self.local_artifacts = {}
        self.store = ScopedStore(self)
        self.secrets = ScopedSecrets(self)
        self.settings = SimpleNamespace(state_dir=self.state_dir, max_workers=1,
            browser_headless=os.environ.get('ORE_BROWSER_HEADLESS', 'true').lower() != 'false',
            browser_executable=os.environ.get('ORE_BROWSER_EXECUTABLE'),
            browser_backend=os.environ.get('ORE_BROWSER_BACKEND', 'playwright'),
            desktop_image=os.environ.get('ORE_DESKTOP_IMAGE', 'ore-desktop:0.2.0rc1'),
            desktop_host_state_dir=os.environ.get('ORE_DESKTOP_HOST_STATE_DIR'),
            desktop_resource_mode=os.environ.get('ORE_DESKTOP_RESOURCE_MODE', 'cgroup'),
            browser_proxy=assignment['profile'].get('proxy'))
        self.limiter = RateLimiter(self.store)
        from .validation import make_vault
        self.vault = make_vault(self.state_dir / 'vault')
        self.coverage = ScopedCoverage(self)
        self.execution = SimpleNamespace(enabled=False)
        self.browser = BrowserManager(self.settings, self.limiter, on_event=self.event,
            store=self.store, secrets=self.secrets)
        self.runtime = ToolRuntime(self, assignment['job_id'], assignment.get('task'))

    def envelope(self):
        return {'assignment_id': self.assignment['id'], 'capability': self.assignment['capability'], 'command_id': self.command_id}

    @staticmethod
    def check(response):
        if response.status_code == 409:
            raise LeaseLost('Coordinator rejected a stale executor operation')
        if response.status_code in (401, 403):
            raise AccessDenied('Coordinator rejected an out-of-scope executor operation')
        response.raise_for_status()

    def request_cancel(self):
        self.cancel_event.set()

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise LeaseLost('Executor operation was interrupted')

    @contextlib.contextmanager
    def io_operation(self):
        with self._io_lock:
            self.check_cancelled()
            self._io_active += 1
        try:
            yield
        finally:
            with self._io_lock:
                self._io_active -= 1

    async def wait_io_settled(self):
        while True:
            with self._io_lock:
                pending = self._io_active
            if not pending:
                return
            await asyncio.sleep(0.05)

    async def sync_operation(self, function, *args):
        # Shield the actual thread; cancellation must not manufacture settlement.
        work = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            self.request_cancel()
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            with contextlib.suppress(BaseException):
                work.result()
            raise

    def rpc(self, method, *args, **kwargs):
        with self.io_operation():
            response = self.client.post(self.prefix + '/rpc', json=pack(self.envelope() | {'method': method, 'args': args, 'kwargs': kwargs}))
            self.check(response)
            self.check_cancelled()
            return response.json()['result']

    def event(self, job_id, kind, payload):
        return self.rpc('event', job_id, kind, payload)

    def profile(self, mission):
        return self.assignment['profile']

    def ensure_artifact(self, artifact_id):
        with self.io_operation():
            return self._ensure_artifact(artifact_id)

    def _ensure_artifact(self, artifact_id):
        if artifact_id in self.local_artifacts:
            return
        rows = self.rpc('artifacts', self.assignment['job_id'])
        artifact = next((item for item in rows if item['id'] == artifact_id), None)
        if not artifact:
            raise AccessDenied('Unknown artifact')
        path = self.state_dir / 'staging' / ('input-' + uuid.uuid4().hex)
        digest = hashlib.sha256()
        maximum = self.assignment['job']['mission'].get('limits', {}).get('max_artifact_bytes', 256 * 1024 * 1024)
        count = 0
        try:
            with self.client.stream('POST', self.prefix + '/artifacts/' + artifact_id, json=self.envelope(), timeout=5) as response:
                self.check(response)
                with path.open('xb') as stream:
                    for chunk in response.iter_bytes():
                        self.check_cancelled()
                        count += len(chunk)
                        if count > maximum:
                            raise AccessDenied('Artifact input exceeds executor limit')
                        digest.update(chunk)
                        stream.write(chunk)
            self.check_cancelled()
            if digest.hexdigest() != artifact['sha256']:
                raise AccessDenied('Artifact transfer SHA-256 mismatch')
            self.local_artifacts[artifact_id] = path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def summaries(self):
        rows = []
        for session in self.browser.sessions.values():
            row = await self.browser.summary(session)
            rows.append({**row, 'closed': session.closed, 'agent_id': session.agent_id,
                'profile_id': session.profile_id, 'executor_isolation': 'process-private-state'})
        return rows

    async def source_check(self, payload):
        from ore_scholarly import search, resolve
        source, operation = payload['source'], payload.get('operation', 'search')
        profile = self.assignment['profile']
        config = dict(profile.get('sources', {}).get(source, {}))
        config['secret_resolver'] = self.secrets.get
        policy = source_probe_policy(profile, source, operation)
        transport = GuardedTransport(policy, self.limiter, float(profile.get('api_interval', 1)),
            profile_id=profile['id'], transport=httpx.AsyncHTTPTransport(proxy=profile.get('proxy')))
        async with httpx.AsyncClient(transport=transport, timeout=60, follow_redirects=False) as client:
            config['client'] = client
            if operation == 'search':
                if not payload.get('query'):
                    raise AccessDenied('An explicit bounded source-check query is required')
                result = await search(source, payload['query'], limit=1, config=config)
            elif operation == 'resolve':
                if not payload.get('identifier'):
                    raise AccessDenied('An explicit source-check identifier is required')
                result = await resolve(source, payload['identifier'], config=config)
            else:
                raise AccessDenied('Source check supports search or resolve only')
        self.event(self.assignment['job_id'], 'source_check', {'source': source, 'operation': operation, 'executor_id': self.prefix.split('/')[-1], 'status': 'response_received'})
        return result

    async def execute(self, command):
        self.command_id = command['id']
        kind, payload = command['kind'], command['payload']
        if kind == 'tool':
            if payload['name'] not in EXECUTOR_TOOLS:
                raise AccessDenied('Tool is not an execution-plane operation')
            if payload['name'] in ('extract', 'archive_expand'):
                await self.sync_operation(self.ensure_artifact, payload['arguments']['artifact_id'])
                # File-only tools can own a separate loop; their extractor thread
                # and synchronous upload must settle before this command does.
                result = await self.sync_operation(lambda: asyncio.run(self.runtime.execute(payload['name'], payload['arguments'])))
            else:
                result = await self.runtime.execute(payload['name'], payload['arguments'])
            return {'result': result, 'image_url': self.runtime.last_image}
        if kind == 'source_check':
            return await self.source_check(payload)
        if kind == 'create_session':
            session = await self.browser.create(self.assignment['job_id'], self.assignment['job']['mission'],
                self.assignment['profile'], agent_id=payload.get('agent_id'))
            return await self.browser.summary(session)
        if kind == 'close':
            await self.browser.close()
            return {'closed': True}
        if kind != 'browser':
            raise AccessDenied('Unknown executor command')
        sid, action, args = payload['session_id'], payload['action'], payload.get('arguments', {})
        session = self.browser.get(sid)
        if action == 'takeover':
            return await self.browser.takeover(sid)
        if action == 'resume':
            return await self.browser.resume(sid)
        if action == 'close':
            await self.browser.close_session(sid)
            return {'closed': True, 'session_id': sid}
        if action == 'save_profile':
            await self.browser.save_profile(session)
            return {'saved': True}
        if action == 'observe':
            return await self.browser.observe(sid, owner='human', screenshot=False, owner_id=args.get('owner_id'))
        if action == 'action':
            return await self.browser.action(sid, args['action'], args, owner=args.get('owner', 'human'), owner_id=args.get('owner_id'))
        if action in ('navigate', 'click', 'type', 'key', 'scroll', 'wait', 'tab', 'back'):
            return await self.browser.action(sid, action, args, owner=args.get('owner', 'human'))
        if action == 'source_check':
            return await self.source_check(args)
        if action == 'verify_challenge':
            async with session.lock:
                await self.browser._authorize(session, 'human', args.get('epoch'))
                challenge_id = args.get('challenge_id') or session.challenge_id
                episode = await asyncio.to_thread(self.store.get_challenge, challenge_id) if challenge_id else None
                checkpoint = args.get('checkpoint_url')
                if checkpoint:
                    parsed = urlsplit(checkpoint)
                    origin = f'{parsed.scheme}://{parsed.netloc}'
                    if episode and episode.get('origin') != origin:
                        raise AccessDenied('Checkpoint does not belong to the challenged origin')
                    session.challenge_url, session.challenge_origin = checkpoint, origin
                elif episode:
                    session.challenge_origin = episode['origin']
                recovered, evidence = await self.browser._target_recovered(session)
                if recovered and challenge_id:
                    await asyncio.to_thread(self.store.resolve_challenge, challenge_id, evidence)
                return {'resolved': recovered, 'evidence': evidence}
        if action in ('secret-fill', 'secret-capture'):
            async with session.lock:
                await self.browser._authorize(session, 'human', args.get('epoch'))
                locator = session.page.locator(args['selector'])
                if action == 'secret-fill':
                    value = await asyncio.to_thread(self.secrets.get, args['ref'])
                    if value is None:
                        raise AccessDenied('Secret reference is unavailable')
                    await locator.fill(value)
                    return {'filled': True}
                value = await locator.input_value() if await locator.evaluate('(el)=>"value" in el') else await locator.inner_text()
                await asyncio.to_thread(self.secrets.set, args['ref'], value.strip())
                return {'captured': True}
        raise AccessDenied('Unknown browser control operation')

    async def close(self):
        try:
            await self.wait_io_settled()
            await self.browser.close()
        finally:
            self.client.close()
            shutil.rmtree(self.state_dir, ignore_errors=True)


async def run_executor(server, *, enrollment_token=None, worker_id=None, once=False):
    """Run one active browser/network assignment in a private executor process."""
    server = server.rstrip('/')
    enrollment_token = enrollment_token or os.environ.get('ORE_EXECUTOR_ENROLLMENT_TOKEN')
    if not enrollment_token:
        raise ValueError('ORE_EXECUTOR_ENROLLMENT_TOKEN is required')
    ident = worker_id or os.environ.get('ORE_EXECUTOR_ID') or 'executor-' + socket.gethostname()
    base_dir = Path(os.environ.get('ORE_EXECUTOR_STATE_DIR', '/tmp/ore-executor')).resolve()
    base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    boot_id = uuid.uuid4().hex
    async with httpx.AsyncClient(base_url=server, timeout=30) as client:
        response = await client.post('/v1/execution/register', headers={'authorization': 'Bearer ' + enrollment_token}, json={
            'id': ident, 'boot_id': boot_id, 'network_zone': os.environ.get('ORE_EXECUTOR_NETWORK_ZONE', 'default'),
            'capabilities': sorted(EXECUTOR_TOOLS), 'runtime': {'hostname': socket.gethostname(),
                'pid': os.getpid(), 'container_declared': os.environ.get('ORE_CONTAINER_EXECUTOR') == '1'}})
        response.raise_for_status()
        grant = response.json()
        client.headers['authorization'] = 'Bearer ' + grant['token']
        prefix = f'/v1/execution/workers/{ident}'
        context, active, active_id = None, None, None

        loop = asyncio.get_running_loop()
        def cancel_commands(commands):
            running, current_id, owner = active, active_id, context
            if running is not None and (commands is None or current_id in commands):
                if owner is not None:
                    owner.request_cancel()
                if not loop.is_closed():
                    loop.call_soon_threadsafe(running.cancel)

        heartbeat = ExecutorHeartbeat(server, grant['token'], prefix, cancel_commands)
        outbox = ReceiptOutbox(base_dir, ident, boot_id)

        async def frames():
            seen = {}
            coordinator_boot = None
            while True:
                await asyncio.sleep(0.2)
                if not context:
                    continue
                try:
                    envelope = context.envelope()
                    summaries = await context.summaries()
                    response = await client.post(prefix + '/sessions', json=envelope | {'sessions': summaries})
                    response.raise_for_status()
                    current_boot = response.json().get('coordinator_boot_id')
                    if current_boot != coordinator_boot:
                        seen.clear()
                        coordinator_boot = current_boot
                    for session in list(context.browser.sessions.values()):
                        if session.closed or not session.frame or seen.get(session.id) == session.frame_id:
                            continue
                        response = await client.post(prefix + '/frame', json=envelope | {'frame': {**session.frame, 'session_id': session.id}})
                        response.raise_for_status()
                        seen[session.id] = session.frame_id
                except (httpx.HTTPError, AccessDenied, LeaseLost):
                    pass

        heartbeat.start()
        frame_task = asyncio.create_task(frames())
        try:
            while True:
                if not await outbox.flush(client, prefix):
                    await asyncio.sleep(1)
                    continue
                try:
                    response = await client.post(prefix + '/poll', json={})
                    response.raise_for_status()
                except (httpx.HTTPError, OSError):
                    await asyncio.sleep(1)
                    continue
                offer = response.json()
                if not offer['command']:
                    if once:
                        return
                    continue
                command, assignment = offer['command'], offer['assignment']
                if context and context.assignment['id'] != assignment['id']:
                    await context.close()
                    context = None
                if context is None:
                    context = ExecutionContext(server, ident, grant['token'], assignment, base_dir)
                else:
                    context.assignment = assignment
                    context.runtime = ToolRuntime(context, assignment['job_id'], assignment.get('task'))
                context.cancel_event.clear()
                context.command_id = command['id']
                active_id = command['id']
                active = asyncio.create_task(context.execute(command))
                try:
                    result = await active
                    context.check_cancelled()
                    report = context.envelope() | {'result': result, 'sessions': await context.summaries()}
                except asyncio.CancelledError:
                    context.request_cancel()
                    report = context.envelope() | {'error': {'code': 'interrupted', 'message': 'Executor operation interrupted'}}
                except Exception as exc:
                    sensitive = command.get('payload', {}).get('action') in ('secret-fill', 'secret-capture')
                    report = context.envelope() | {'error': {'code': 'interrupted' if context.cancel_event.is_set() else type(exc).__name__,
                        'message': type(exc).__name__ if sensitive else str(exc)[:500]}}
                finally:
                    # Cancelled to_thread I/O may still own a socket or upload;
                    # keep reporting physical interruption as unconfirmed until it exits.
                    await context.wait_io_settled()
                    active, active_id = None, None
                outbox.enqueue(report)
                while not await outbox.flush(client, prefix):
                    await asyncio.sleep(1)
                if command['kind'] == 'close':
                    await context.close()
                    context = None
                if once:
                    return
        finally:
            frame_task.cancel()
            if active:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            await asyncio.gather(frame_task, return_exceptions=True)
            await heartbeat.close()
            if context:
                await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default=os.environ.get('ORE_COORDINATOR_URL', 'http://coordinator:8765'))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    asyncio.run(run_executor(args.server, once=args.once))


if __name__ == '__main__':
    main()
