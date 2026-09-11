"""Coordinator-owned dispatch to isolated browser/network executor processes.

Executors connect outbound and receive scoped assignment capabilities. They never
receive a coordinator operator token, database DSN, or Codex authentication files.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import canonical_digest
from .policy import AccessDenied, redact
from .store import LeaseLost

EXECUTOR_TOOLS = frozenset({'browser_open', 'browser_observe', 'browser_action', 'search', 'resolve',
    'fetch', 'download', 'artifact_commit', 'archive_expand', 'extract', 'page_extract', 'challenge', 'handoff'})


class ExecutorUnavailable(AccessDenied):
    code = 'executor_unavailable'


def unpack(value):
    if isinstance(value, dict):
        if set(value) == {'__ore_bytes__'}:
            return base64.b64decode(value['__ore_bytes__'], validate=True)
        return {key: unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack(item) for item in value]
    return value


def task_lease(task):
    return {'task_id': task['id'], 'worker_id': task['worker_id'], 'fence': task['fence'], 'revision': task['revision']} if task else None


@dataclass
class RemoteSession:
    id: str
    job_id: str
    worker_id: str
    assignment_id: str
    control: str = 'agent'
    epoch: int = 1
    frame: dict | None = None
    subscribers: set = field(default_factory=set)
    closed: bool = False
    metadata: dict = field(default_factory=dict)


class ExecutionManager:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.boot_id = uuid.uuid4().hex
        self.enabled = getattr(engine.settings, 'execution_backend', os.environ.get('ORE_EXECUTION_BACKEND', 'local')) == 'remote'
        self.enrollment_token = getattr(engine.settings, 'executor_enrollment_token', None) or os.environ.get('ORE_EXECUTOR_ENROLLMENT_TOKEN')
        self.network_zone = getattr(engine.settings, 'executor_network_zone', 'default')
        self.sessions_by_id: dict[str, RemoteSession] = {}
        self.lock = asyncio.Lock()
        self.on_session_lost = None
        self.monitor = None
        self.stopping = False

    def authorize_request(self, request):
        """Authenticate explicit channel routes; never grant prefix-wide access."""
        import re
        path, method = request.url.path, request.method.upper()
        header = request.headers.get('authorization', '')
        token = header[7:] if header.lower().startswith('bearer ') else ''
        if path == '/v1/execution/register':
            return bool(method == 'POST' and ((self.enrollment_token and secrets.compare_digest(token, self.enrollment_token)) or (getattr(getattr(self, 'engine', None), 'pool', None) and self.engine.pool.valid_enrollment(token))))
        match = re.fullmatch(r'/v1/execution/workers/([^/]+)/(heartbeat|poll|result|cancelled|sessions|frame|rpc|uploads|uploads/[a-f0-9]{32}|artifacts/[^/]+)', path)
        if not match:
            return False
        action = match.group(2)
        if method != ('PUT' if action.startswith('uploads/') else 'POST'):
            return False
        try:
            self.authenticate(match.group(1), token)
            return True
        except AccessDenied:
            return False

    def _get(self, collection, key):
        return self.store.get_document('execution.' + collection, key)

    def _put(self, collection, key, data, job_id=None):
        return self.store.put_document('execution.' + collection, key, data, job_id=job_id)

    def _list(self, collection):
        return self.store.list_documents('execution.' + collection, all_generations=True)

    def _start_monitor(self):
        if not self.monitor or self.monitor.done():
            self.stopping = False
            self.monitor = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while not self.stopping:
            await asyncio.sleep(5)
            for worker in self._list('worker'):
                if worker.get('state') != 'lost' and time.time() - worker.get('last_seen', 0) > 40:
                    await self.worker_lost(worker['id'], 'executor_heartbeat_expired')

    async def start(self):
        self._start_monitor()

    async def close(self):
        self.stopping = True
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None

    def available(self):
        return sum(1 for item in self._list('worker') if item.get('state') == 'idle'
            and item.get('network_zone') == self.network_zone and time.time() - item.get('last_seen', 0) < 30)

    async def register(self, enrollment, data):
        ordinary = bool(self.enrollment_token and secrets.compare_digest(enrollment, self.enrollment_token))
        pool = getattr(self.engine, 'pool', None)
        launch = None
        if not ordinary:
            if not pool: raise AccessDenied('Invalid executor enrollment token')
            launch = pool.consume_enrollment(enrollment, data.get('id'), data.get('network_zone', 'default'))
        self._start_monitor()
        ident = data.get('id') or 'executor-' + uuid.uuid4().hex
        if not isinstance(ident, str) or not 1 <= len(ident) <= 200:
            raise AccessDenied('Invalid executor identity')
        if self._get('worker', ident):
            await self.worker_lost(ident, 'executor_boot_replaced')
        token = secrets.token_urlsafe(40)
        row = {'id': ident, 'boot_id': data.get('boot_id') or uuid.uuid4().hex,
            'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'state': 'idle',
            'last_seen': time.time(), 'network_zone': data.get('network_zone', 'default'),
            'capabilities': data.get('capabilities', []), 'runtime': redact(data.get('runtime', {})),
            'assignment_id': None, 'idle_since': time.time(), 'pool_broker_id': launch['broker_id'] if launch else None}
        self._put('worker', ident, row)
        return {'id': ident, 'boot_id': row['boot_id'], 'token': token, 'heartbeat_seconds': 5}

    def authenticate(self, worker_id, token):
        worker = self._get('worker', worker_id)
        if not worker or worker.get('state') == 'lost' or not secrets.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(), worker.get('token_sha256', '')):
            raise AccessDenied('Invalid executor capability')
        return worker

    def touch(self, worker):
        self._start_monitor()
        worker = self._get('worker', worker['id'])
        if not worker or worker.get('state') == 'lost':
            raise AccessDenied('Executor registration was revoked')
        return self._put('worker', worker['id'], {**worker, 'last_seen': time.time()})

    async def worker_lost(self, worker_id, reason):
        worker = self._get('worker', worker_id)
        if worker:
            self._put('worker', worker_id, {**worker, 'state': 'lost'})
        for row in self._list('assignment'):
            if row['worker_id'] == worker_id and row['status'] not in ('closed', 'lost'):
                self._put('assignment', row['id'], {**row, 'status': 'lost'}, row['job_id'])
        for row in self._list('session'):
            if row['worker_id'] != worker_id or row.get('closed'):
                continue
            row.update(closed=True, loss_reason=reason)
            self._put('session', row['id'], row, row['job_id'])
            local = self.sessions_by_id.get(row['id'])
            if local:
                local.closed = True
            self.engine.event(row['job_id'], 'executor.session_lost', {'session_id': row['id'], 'worker_id': worker_id, 'reason': reason})
            if self.on_session_lost:
                result = self.on_session_lost(row, reason)
                if inspect.isawaitable(result):
                    await result

    async def _assignment(self, task, job_id, profile=None, operator=False, recovery_task_id=None, needs_browser=True):
        self._start_monitor()
        if task:
            self.store.validate_task_lease(task['id'], task['worker_id'], task['fence'], task['revision'])
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if task and task['job_id'] != job_id:
            raise AccessDenied('Task belongs to another job')
        async with self.lock:
            if recovery_task_id:
                for row in self._list('assignment'):
                    bound = (row.get('task') or {}).get('id') or row.get('recovery_task_id')
                    if bound == recovery_task_id and row['status'] == 'active':
                        raise ExecutorUnavailable('This task already has an active executor assignment')
            if task:
                for row in self._list('assignment'):
                    bound = (row.get('task') or {}).get('id') or row.get('recovery_task_id')
                    if (bound == task['id'] and row['status'] == 'active' and row['job_id'] == job_id
                            and row['revision'] == job['revision'] and row['generation'] == job['generation']):
                        if not row.get('task') or row['task']['fence'] != task['fence']:
                            sessions = [item for item in self._list('session') if item['assignment_id'] == row['id'] and not item.get('closed')]
                            if any(item.get('control') == 'human' for item in sessions):
                                raise ExecutorUnavailable('The user still controls this task browser')
                            for command in self._list('command'):
                                if command['assignment_id'] == row['id'] and command['status'] in ('queued', 'running'):
                                    self._put('command', command['id'], {**command, 'status': 'cancel_requested'}, job_id)
                            capability = secrets.token_urlsafe(40)
                            row = {**row, 'task': task, 'operator': False, 'capability_sha256': hashlib.sha256(capability.encode()).hexdigest()}
                            self._put('assignment', row['id'], row, job_id)
                            self.engine.secrets.set('executor-assignment:' + row['id'], capability)
                        if needs_browser: row = self._reserve_browser(row)
                        return row
            workers = [row for row in self._list('worker') if row.get('state') == 'idle'
                and row.get('network_zone') == self.network_zone and time.time() - row.get('last_seen', 0) < 30]
            if not workers:
                raise ExecutorUnavailable('No isolated executor is currently available')
            profile = profile or self.engine.profile(job['mission'])
            principal = profile.get('principal_id', 'operator')
            profile_key = canonical_digest([principal, profile.get('id', 'public')])
            cap = int(profile.get('max_executor_sessions', getattr(self.engine.settings, 'scheduler_global_limit', 64)))
            in_use = [row for row in self._list('assignment') if row['status'] == 'active' and row.get('profile_key') == profile_key]
            if len(in_use) >= cap:
                raise ExecutorUnavailable('Access-profile executor concurrency limit reached')
            worker = workers[0]
            ident, capability = uuid.uuid4().hex, secrets.token_urlsafe(40)
            row = {'id': ident, 'job_id': job_id, 'task': task, 'worker_id': worker['id'],
                'boot_id': worker['boot_id'], 'capability_sha256': hashlib.sha256(capability.encode()).hexdigest(),
                'status': 'active', 'operator': operator, 'recovery_task_id': recovery_task_id,
                'revision': job['revision'], 'generation': job['generation'],
                'profile': profile, 'profile_key': profile_key, 'browser_reserved': False, 'session_ids': [], 'created_at': time.time()}
            if needs_browser: row = self._reserve_browser(row)
            else: self._put('assignment', ident, row, job_id)
            self._put('worker', worker['id'], {**worker, 'state': 'busy', 'assignment_id': ident})
            # Only encrypted local secret storage retains the reusable capability.
            self.engine.secrets.set('executor-assignment:' + ident, capability)
            return row

    def _reserve_browser(self, assignment):
        """Profile browser capacity is global, separate from stateless API slots."""
        from sqlalchemy import select
        from .store import documents
        if assignment.get('browser_reserved'): return assignment
        profile = assignment['profile']
        cap = int(profile.get('max_browser_sessions', 5 if profile.get('id', 'public') == 'public' else 1))
        with self.store._tx() as conn:
            old = self.store._doc(conn, 'execution.assignment', assignment['id'])
            if old and old['data'].get('browser_reserved'): return old['data']
            active = [r['data'] for r in conn.execute(select(documents).where(documents.c.collection == 'execution.assignment')).mappings()
                      if r['data']['status'] == 'active' and r['data'].get('profile_key') == assignment['profile_key']
                      and r['data'].get('browser_reserved', bool(r['data'].get('session_ids')))]
            if len(active) >= cap: raise ExecutorUnavailable('Access-profile browser concurrency limit reached')
            row = {**assignment, 'browser_reserved': True}
            self.store._put(conn, 'execution.assignment', row['id'], row, self.store._job(conn, row['job_id']))
            return row

    def validate_assignment(self, worker, assignment_id, capability, require_task=True):
        row = self._get('assignment', assignment_id)
        if not row or row['status'] != 'active' or row['worker_id'] != worker['id'] or row['boot_id'] != worker['boot_id']:
            raise LeaseLost('Executor assignment is unavailable or superseded')
        if not secrets.compare_digest(hashlib.sha256(capability.encode()).hexdigest(), row['capability_sha256']):
            raise AccessDenied('Invalid assignment capability')
        job = self.store.get_job(row['job_id'])
        if not job or job['revision'] != row['revision'] or job['generation'] != row['generation']:
            raise LeaseLost('Executor assignment belongs to an obsolete mission')
        if require_task and not row.get('task'):
            raise LeaseLost('Operator browser has no active task lease')
        if require_task and row.get('task'):
            task = row['task']
            self.store.validate_task_lease(task['id'], task['worker_id'], task['fence'], task['revision'])
        return row

    async def _command(self, assignment, kind, payload, *, operator=False, timeout=180):
        worker = self._get('worker', assignment['worker_id'])
        if not worker or worker['state'] == 'lost' or time.time() - worker['last_seen'] > 40:
            raise ExecutorUnavailable('Assigned executor is unavailable')
        ident = uuid.uuid4().hex
        row = {'id': ident, 'assignment_id': assignment['id'], 'worker_id': worker['id'], 'boot_id': worker['boot_id'],
            'job_id': assignment['job_id'], 'kind': kind, 'payload': payload, 'operator': operator,
            'task_fence': (assignment.get('task') or {}).get('fence'),
            'status': 'queued', 'created_at': time.time()}
        self._put('command', ident, row, assignment['job_id'])
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                result = self._get('command', ident)
                if result['status'] == 'completed':
                    return result['result']
                if result['status'] == 'failed':
                    raise AccessDenied(result.get('error', {}).get('message', 'Executor command failed'))
                current = self._get('assignment', assignment['id'])
                if current['status'] != 'active':
                    raise ExecutorUnavailable('Executor assignment was lost')
                await asyncio.sleep(0.1)
            raise ExecutorUnavailable('Executor command timed out; outcome requires re-observation')
        except BaseException:
            current = self._get('command', ident)
            if current and current['status'] not in ('completed', 'failed'):
                self._put('command', ident, {**current, 'status': 'cancel_requested'}, assignment['job_id'])
            raise

    async def execute(self, task, job_id, name, args):
        if name not in EXECUTOR_TOOLS:
            raise AccessDenied('Tool does not belong to the execution plane')
        assignment = await self._assignment(task, job_id, needs_browser=name.startswith('browser_') or bool(args.get('session_id')) or name in ('challenge', 'handoff', 'page_extract'))
        return await self._command(assignment, 'tool', {'name': name, 'arguments': args})

    async def source_check(self, profile, source, operation='search', *, query=None, identifier=None):
        job = self.store.create_job({'goal': f'Explicit {source} {operation} connectivity check',
            'kind': 'source_check', 'access_profile': profile['id'], 'artifact_roles': []})
        self.store.update_job(job['id'], status='draft')
        assignment = None
        try:
            assignment = await self._assignment(None, job['id'], profile, operator=True, needs_browser=False)
            result = await self._command(assignment, 'source_check', {'name': operation, 'source': source,
                'operation': operation, 'query': query, 'identifier': identifier}, operator=True)
            self.store.update_job(job['id'], status='completed', source_check={'source': source, 'operation': operation, 'response_received': True})
            return result
        except BaseException:
            self.store.update_job(job['id'], status='needs_review')
            raise
        finally:
            if assignment:
                await self._release(assignment)

    async def create_session(self, job_id, mission=None, profile=None, agent_id=None, *, task_id=None):
        if task_id is None and agent_id and agent_id.startswith('task:'):
            task_id = agent_id.removeprefix('task:')
        if task_id:
            job, original = self.store.get_job(job_id), self.store.get_task(task_id)
            if not job or not original or original['job_id'] != job_id:
                raise AccessDenied('Recreated browser task belongs to another job')
            if original['revision'] != job['revision'] or original['generation'] != job['generation']:
                raise LeaseLost('Recreated browser task belongs to an obsolete mission')
            expected_actor = 'task:' + task_id
            if agent_id is not None and agent_id != expected_actor:
                raise AccessDenied('Recreated browser actor does not match its task')
            agent_id = expected_actor
        # The operator may recreate a waiting task browser without acquiring its
        # expired model lease. Only a later valid claim grants task authority.
        assignment = await self._assignment(None, job_id, profile, operator=True, recovery_task_id=task_id)
        return await self._command(assignment, 'create_session', {'agent_id': agent_id}, operator=True)

    def has_session(self, sid):
        return bool(self._get('session', sid))

    def get_session(self, sid):
        row = self._get('session', sid)
        if not row:
            raise AccessDenied('Unknown remote browser session')
        if sid not in self.sessions_by_id:
            self.sessions_by_id[sid] = RemoteSession(sid, row['job_id'], row['worker_id'], row['assignment_id'])
        value = self.sessions_by_id[sid]
        value.control, value.epoch, value.closed, value.metadata = row.get('control', 'agent'), row.get('epoch', 1), row.get('closed', False), row
        return value

    async def session_summary(self, sid):
        return dict(self.get_session(sid).metadata)

    async def list_sessions(self):
        return [dict(row) for row in self._list('session') if not row.get('closed')]

    async def task_sessions(self, job_id, task_id=None):
        """Discover this job's current browsers through trusted assignment ownership."""
        job = self.store.get_job(job_id)
        if not job:
            return []
        assignments = {}
        for row in self._list('assignment'):
            if (row['job_id'] != job_id or row['status'] != 'active'
                    or row['revision'] != job['revision'] or row['generation'] != job['generation']):
                continue
            owner = (row.get('task') or {}).get('id') or row.get('recovery_task_id')
            if task_id is not None and owner != task_id:
                continue
            assignments[row['id']] = row
        return [dict(row) for row in self._list('session')
            if row['job_id'] == job_id and not row.get('closed') and row['assignment_id'] in assignments]

    async def browser_command(self, sid, action, args=None):
        session = self.get_session(sid)
        if session.closed:
            raise ExecutorUnavailable('Remote browser session was lost; create a new session')
        assignment = self._get('assignment', session.assignment_id)
        result = await self._command(assignment, 'browser', {'session_id': sid, 'action': action, 'arguments': args or {}}, operator=True)
        if action == 'close':
            await self._release(assignment)
        return result

    async def release_task(self, task):
        if not task:
            return
        for row in self._list('assignment'):
            owned = row.get('task') or {}
            if (owned.get('id') == task['id'] and owned.get('fence') == task['fence']
                    and owned.get('revision') == task['revision'] and row['status'] == 'active'):
                sessions = [s for s in self._list('session') if s['assignment_id'] == row['id'] and not s.get('closed')]
                if any(s.get('control') == 'human' for s in sessions):
                    continue
                await self._release(row)

    async def _release(self, assignment):
        try:
            await self._command(assignment, 'close', {}, operator=True, timeout=20)
        except AccessDenied:
            pass
        self._put('assignment', assignment['id'], {**assignment, 'status': 'closed'}, assignment['job_id'])
        for row in self._list('session'):
            if row['assignment_id'] == assignment['id']:
                self._put('session', row['id'], {**row, 'closed': True}, row['job_id'])
        worker = self._get('worker', assignment['worker_id'])
        if worker and worker['state'] != 'lost':
            self._put('worker', worker['id'], {**worker, 'state': 'idle', 'assignment_id': None, 'idle_since': time.time()})

    def publish_sessions(self, worker, assignment, summaries):
        ids = set(assignment.get('session_ids', []))
        for summary in summaries:
            if summary.get('job_id') != assignment['job_id']:
                raise AccessDenied('Cross-job session report')
            sid = summary['id']
            existing = self._get('session', sid)
            if existing and existing['assignment_id'] != assignment['id']:
                raise AccessDenied('Session belongs to another assignment')
            if existing and int(summary.get('epoch', 0)) < int(existing.get('epoch', 0)):
                continue
            row = {**summary, 'worker_id': worker['id'], 'boot_id': worker['boot_id'],
                'assignment_id': assignment['id'], 'closed': bool(summary.get('closed', False))}
            self._put('session', sid, row, assignment['job_id'])
            ids.add(sid)
            self.get_session(sid)
        self._put('assignment', assignment['id'], {**assignment, 'session_ids': sorted(ids)}, assignment['job_id'])

    def publish_frame(self, worker, assignment, frame):
        sid = frame.get('session_id')
        if sid not in assignment.get('session_ids', []):
            raise AccessDenied('Frame belongs to an unregistered session')
        session = self.get_session(sid)
        if session.closed or frame.get('epoch') != session.epoch:
            return
        session.frame = {**frame, 'executor_id': worker['id'], 'boot_id': worker['boot_id']}
        for queue in list(session.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(session.frame)


def create_execution_router(manager):
    from .execution_api import create_execution_router as factory
    return factory(manager)
