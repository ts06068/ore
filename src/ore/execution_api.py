"""Role-scoped executor channel with exact capability-aware authentication.

Every endpoint authenticates either enrollment or a worker capability; assignment
operations additionally bind boot, task fence, revision and command identity.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .execution import task_lease, unpack
from .models import canonical_digest
from .policy import AccessDenied, redact
from .store import LeaseLost


def create_execution_router(manager):
    router = APIRouter(prefix='/v1/execution')
    engine, store = manager.engine, manager.store

    def bearer(request):
        value = request.headers.get('authorization', '')
        return value[7:] if value.lower().startswith('bearer ') else ''

    def auth(worker_id, request):
        return manager.authenticate(worker_id, bearer(request))

    def assignment_for(worker, data, require_task=True):
        return manager.validate_assignment(worker, data['assignment_id'], data['capability'], require_task=require_task)

    def context(worker, data):
        command = manager._get('command', data.get('command_id'))
        if not command or command['worker_id'] != worker['id'] or command['boot_id'] != worker['boot_id'] or command['assignment_id'] != data.get('assignment_id'):
            raise AccessDenied('RPC is not bound to an assigned command')
        if command['status'] in ('cancel_requested', 'cancelled', 'failed'):
            raise LeaseLost('Command has been interrupted')
        assignment = assignment_for(worker, data, require_task=not command.get('operator'))
        if not command.get('operator') and command.get('task_fence') is not None and command['task_fence'] != (assignment.get('task') or {}).get('fence'):
            raise LeaseLost('Command belongs to an earlier task attempt')
        return assignment, command

    @router.post('/register')
    async def register(request: Request):
        return await manager.register(bearer(request), await request.json())

    @router.post('/workers/{worker_id}/heartbeat')
    async def heartbeat(worker_id: str, request: Request):
        worker = auth(worker_id, request)
        manager.touch(worker)
        cancellations = []
        for command in manager._list('command'):
            if command['worker_id'] != worker_id or command['boot_id'] != worker['boot_id'] or command['status'] not in ('running', 'cancel_requested'):
                continue
            if command['status'] == 'cancel_requested':
                cancellations.append(command['id'])
                continue
            assignment = manager._get('assignment', command['assignment_id'])
            if not assignment or assignment['status'] != 'active':
                cancellations.append(command['id'])
                continue
            if not command.get('operator') and assignment.get('task'):
                task = assignment['task']
                try:
                    store.validate_task_lease(task['id'], task['worker_id'], task['fence'], task['revision'])
                except LeaseLost:
                    cancellations.append(command['id'])
                else:
                    if store.get_job(assignment['job_id'])['status'] not in ('queued', 'running', 'resuming'):
                        cancellations.append(command['id'])
        return {'status': 'ok', 'cancel_commands': cancellations}

    @router.post('/workers/{worker_id}/poll')
    async def poll(worker_id: str, request: Request):
        worker = auth(worker_id, request)
        manager.touch(worker)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for command in manager._list('command'):
                if command['worker_id'] != worker_id or command['boot_id'] != worker['boot_id'] or command['status'] != 'queued':
                    continue
                assignment = manager._get('assignment', command['assignment_id'])
                if not assignment or assignment['status'] != 'active':
                    continue
                manager._put('command', command['id'], {**command, 'status': 'running'}, command['job_id'])
                capability = engine.secrets.get('executor-assignment:' + assignment['id'])
                return {'command': command, 'assignment': {**assignment, 'capability': capability,
                    'job': store.get_job(assignment['job_id'])}}
            await asyncio.sleep(0.15)
        return {'command': None}

    @router.post('/workers/{worker_id}/cancelled')
    async def cancelled(worker_id: str, request: Request):
        # A cancellation receipt has no authority to write artifacts or accept a
        # result. It must remain deliverable after the task fence is revoked.
        worker = auth(worker_id, request)
        data = await request.json()
        command = manager._get('command', data.get('command_id'))
        assignment = manager._get('assignment', data.get('assignment_id'))
        if (not command or not assignment or command['worker_id'] != worker_id
                or command['boot_id'] != worker['boot_id']
                or command['assignment_id'] != assignment['id']
                or assignment['worker_id'] != worker_id or assignment['boot_id'] != worker['boot_id']):
            raise AccessDenied('Cancellation is not bound to this worker command')
        digest = hashlib.sha256(str(data.get('capability', '')).encode()).hexdigest()
        if not secrets.compare_digest(digest, assignment['capability_sha256']):
            raise AccessDenied('Invalid assignment capability')
        if command.get('cancel_acknowledged_at'):
            return {'status': 'duplicate', 'command_id': command['id']}
        if command['status'] in ('completed', 'failed'):
            # The final result already established settlement. This is an
            # idempotent acknowledgement only; no result or artifact is changed.
            return {'status': 'settled', 'command_id': command['id']}
        admissible = command['status'] in ('cancel_requested', 'cancelled') or assignment['status'] != 'active'
        if not admissible and assignment.get('task'):
            task = assignment['task']
            try: store.validate_task_lease(task['id'], task['worker_id'], task['fence'], task['revision'])
            except LeaseLost: admissible = True
            if store.get_job(assignment['job_id'])['status'] not in ('queued', 'running', 'resuming'):
                admissible = True
        if not admissible:
            raise LeaseLost('Command has no outstanding interruption')
        if command['status'] not in ('completed', 'failed'):
            manager._put('command', command['id'], {**command, 'status': 'cancelled',
                'cancel_acknowledged_at': time.time(), 'completed_at': time.time()}, command['job_id'])
        manager.touch(worker)
        engine.event(command['job_id'], 'executor.cancel_acknowledged', {'command_id': command['id'], 'worker_id': worker_id})
        workflows = getattr(engine, 'workflows', None)
        if workflows:
            job = store.get_job(command['job_id'])
            if job.get('workflow_run_id'): workflows.reconcile(command['job_id'])
        return {'status': 'acknowledged', 'command_id': command['id']}

    @router.post('/workers/{worker_id}/result')
    async def result(worker_id: str, request: Request):
        worker = auth(worker_id, request)
        data = await request.json()
        assignment, command = context(worker, data)
        if command['status'] == 'completed':
            return {'status': 'duplicate'}
        if data.get('sessions') is not None:
            manager.publish_sessions(worker, assignment, data['sessions'])
        error = data.get('error')
        row = {**command, 'status': 'failed' if error else 'completed', 'completed_at': time.time()}
        if error:
            row['error'] = redact(error)
        else:
            row['result'] = data.get('result')
        manager._put('command', command['id'], row, command['job_id'])
        return {'status': 'recorded'}

    @router.post('/workers/{worker_id}/sessions')
    async def sessions(worker_id: str, request: Request):
        worker = auth(worker_id, request)
        data = await request.json()
        assignment = assignment_for(worker, data, require_task=False)
        manager.publish_sessions(worker, assignment, data.get('sessions', []))
        return {'status': 'recorded', 'coordinator_boot_id': manager.boot_id}

    @router.post('/workers/{worker_id}/frame')
    async def frame(worker_id: str, request: Request):
        worker = auth(worker_id, request)
        data = await request.json()
        assignment = assignment_for(worker, data, require_task=False)
        manager.publish_frame(worker, assignment, data['frame'])
        return {'status': 'ok'}

    @router.post('/workers/{worker_id}/rpc')
    async def rpc(worker_id: str, request: Request):
        worker = auth(worker_id, request)
        data = await request.json()
        assignment, command = context(worker, data)
        method, args, kwargs = data['method'], unpack(data.get('args', [])), unpack(data.get('kwargs', {}))
        job_id, task = assignment['job_id'], assignment.get('task')
        profile = assignment['profile']
        lease = task_lease(task) if task and not command.get('operator') else None
        job_methods = {'get_job', 'resources', 'artifacts', 'tasks', 'observations', 'record_observation',
            'commit_discovery', 'update_job', 'acquire_control', 'request_control', 'release_control', 'validate_control', 'get_control', 'reserve_challenge', 'observe_challenge'}
        if method in job_methods:
            if not args or args[0] != job_id:
                raise AccessDenied('Cross-job executor RPC')
            if method in ('record_observation', 'commit_discovery', 'update_job'):
                kwargs['lease'] = lease
            if method == 'update_job' and set(kwargs) - {'lease', 'status', 'state'}:
                raise AccessDenied('Executor cannot edit coordinator job metadata')
            if method == 'update_job' and (kwargs.get('status') or kwargs.get('state')) not in ('awaiting_user', 'awaiting_auth'):
                raise AccessDenied('Executor can only request a human handoff')
            if method == 'observe_challenge':
                expected = profile.get('id', 'public') + ':' + profile.get('principal_id', 'operator')
                if len(args) not in (3, 4) or args[2] != expected or kwargs:
                    raise AccessDenied('Wrong challenge observation scope')
            if method == 'reserve_challenge':
                expected = profile.get('id', 'public') + ':' + profile.get('principal_id', 'operator')
                if len(args) < 3 or args[2] != expected:
                    raise AccessDenied('Wrong challenge auth context')
                # The coordinator owns attempt ceilings, even for the first reservation.
                mission = store.get_job(job_id)['mission']
                config = mission.get('on_challenge', {})
                attempts = args[3] if len(args) > 3 else kwargs.pop('max_attempts', 3)
                seconds = args[4] if len(args) > 4 else kwargs.pop('max_active_seconds', 120)
                active = args[5] if len(args) > 5 else kwargs.pop('active_seconds', 0)
                if len(args) > 6 or kwargs or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (attempts, seconds, active)):
                    raise AccessDenied('Invalid challenge reservation')
                args = [*args[:3], min(int(attempts), int(config.get('max_attempts_per_episode', 3))),
                    min(float(seconds), float(config.get('max_active_seconds', 120))), active]
            return {'result': getattr(store, method)(*args, **kwargs)}
        if method == 'heartbeat':
            if not task or args[:3] != [task['id'], task['worker_id'], task['fence']]:
                raise AccessDenied('Wrong task heartbeat')
            return {'result': store.heartbeat(*args, **kwargs)}
        if method in ('reserve_budget', 'get_budget'):
            expected = f"{job_id}:{assignment['revision']}:{assignment['generation']}"
            if len(args) < 2 or args[0] != expected:
                raise AccessDenied('Wrong budget scope')
            mission = store.get_job(job_id)['mission']
            ceilings = {'download_bytes': mission.get('budget', {}).get('max_bytes', 1_000_000_000),
                'browser_actions': mission.get('limits', {}).get('max_browser_actions', 1000)}
            if args[1] not in ceilings:
                raise AccessDenied('Executor cannot reserve this budget category')
            if method == 'reserve_budget':
                amount = args[2] if len(args) > 2 else kwargs.pop('amount', None)
                limit = args[3] if len(args) > 3 else kwargs.pop('limit', None)
                if len(args) > 4 or kwargs or not all(isinstance(n, (int, float)) and math.isfinite(n) and n >= 0 for n in (amount, limit)):
                    raise AccessDenied('Invalid budget reservation')
                if args[1] == 'browser_actions' and amount != 1:
                    raise AccessDenied('Each browser input consumes one action')
                args = [*args[:2], amount, min(limit, ceilings[args[1]])]
            return {'result': getattr(store, method)(*args, **kwargs)}
        if method in ('acquire_rate_slot', 'confirm_rate_slot', 'penalize_rate'):
            if not args or not str(args[0]).startswith(profile.get('id', 'public') + ':'):
                raise AccessDenied('Wrong rate scope')
            # One shared host bucket across profiles/containers prevents profile cloning from resetting pacing.
            args[0] = 'executor-origin:' + str(args[0])[len(profile.get('id', 'public')) + 1:]
            lane = kwargs.get('lane', 'main')
            if lane not in ('main', 'browser_resource') or method == 'penalize_rate' and lane != 'main':
                raise AccessDenied('Unknown executor pacing lane')
            if method == 'acquire_rate_slot':
                tool = command.get('payload', {}).get('name', 'browser')
                mission = store.get_job(job_id)['mission']
                if lane == 'browser_resource':
                    from .policy import browser_resource_interval
                    floor = browser_resource_interval(mission, profile)
                else:
                    floor = float(profile.get('api_interval', 1)) if tool in ('search', 'resolve') else float(mission.get('limits', {}).get('origin_min_interval_seconds', 3))
                if len(args) > 1:
                    args[1] = max(float(args[1]), floor)
                else:
                    kwargs['interval'] = max(float(kwargs.get('interval', 0)), floor)
            return {'result': getattr(store, method)(*args, **kwargs)}
        if method in ('finish_challenge', 'resolve_challenge', 'get_challenge', 'adapt_challenge'):
            episode = store.get_challenge(args[0])
            if not episode or episode.get('auth_context') != profile.get('id', 'public') + ':' + profile.get('principal_id', 'operator'):
                raise AccessDenied('Wrong challenge episode')
            return {'result': getattr(store, method)(*args, **kwargs)}
        if method == 'event':
            if args[0] != job_id:
                raise AccessDenied('Cross-job event')
            return {'result': engine.event(*args, **kwargs)}
        if method in ('coverage.capture_snapshot', 'coverage.check_download', 'coverage.bind_artifact', 'coverage.record_attempt', 'coverage.register_candidates'):
            if args[0] != job_id:
                raise AccessDenied('Cross-job evidence')
            if method != 'coverage.check_download':
                kwargs['lease'] = lease
            if len(kwargs.get('content', b'')) > 16 * 1024 * 1024:
                raise AccessDenied('Snapshot exceeds transfer limit')
            return {'result': getattr(engine.coverage, method.split('.')[1])(*args, **kwargs)}
        if method in ('secret.get', 'secret.set', 'secret.merge_browser_profile'):
            if not args:
                raise AccessDenied('Missing scoped secret reference')
            ref = args[0]
            profile_ref = 'browser-profile:' + canonical_digest([profile.get('principal_id', 'operator'), profile.get('id', 'public')])
            if method == 'secret.merge_browser_profile':
                if ref != profile_ref or len(args) != 3 or kwargs:
                    raise AccessDenied('Browser profile merge is outside assignment scope')
                from .browser import merge_browser_profile
                return {'result': merge_browser_profile(engine.secrets, ref, args[1], args[2])}
            allowed_refs = set()
            def visit(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key.endswith('_ref') and isinstance(item, str):
                            allowed_refs.add(item)
                        else:
                            visit(item)
                elif isinstance(value, list):
                    for item in value:
                        visit(item)
            visit(profile)
            extra = command.get('payload', {}).get('arguments', {}).get('ref') if command.get('operator') else None
            if extra:
                allowed_refs.add(extra)
            if method == 'secret.get':
                if ref not in allowed_refs and ref != profile_ref:
                    raise AccessDenied('Secret is outside assignment scope')
                return {'result': engine.secrets.get(ref)}
            if ref != profile_ref and not ref.startswith('browser-download:') and ref != extra:
                raise AccessDenied('Secret write is outside assignment scope')
            engine.secrets.set(ref, args[1])
            return {'result': True}
        raise AccessDenied('Unsupported scoped executor RPC')

    @router.post('/workers/{worker_id}/uploads')
    async def begin_upload(worker_id: str, request: Request):
        worker = auth(worker_id, request)
        data = await request.json()
        assignment, command = context(worker, data)
        if command.get('operator') or not assignment.get('task'):
            raise AccessDenied('Artifact upload requires an active task')
        record = data['record']
        if not re.fullmatch('[a-f0-9]{64}', record.get('sha256', '')):
            raise AccessDenied('Original SHA-256 is required')
        if not any(item['id'] == record.get('resource_id') for item in store.resources(assignment['job_id'])):
            raise AccessDenied('Unknown artifact parent')
        ident = uuid.uuid4().hex
        row = {'id': ident, 'assignment_id': assignment['id'], 'command_id': command['id'],
            'worker_id': worker_id, 'job_id': assignment['job_id'], 'record': record,
            'status': 'pending', 'created_at': time.time()}
        manager._put('upload', ident, row, assignment['job_id'])
        return {'upload_id': ident}

    @router.put('/workers/{worker_id}/uploads/{upload_id}')
    async def upload(worker_id: str, upload_id: str, request: Request):
        worker = auth(worker_id, request)
        row = manager._get('upload', upload_id)
        if not row or row['worker_id'] != worker_id:
            raise AccessDenied('Unknown upload')
        data = {'assignment_id': row['assignment_id'], 'command_id': row['command_id'],
            'capability': request.headers.get('x-ore-assignment', '')}
        assignment, command = context(worker, data)
        if row['status'] == 'committed':
            return row['artifact']
        job = store.get_job(row['job_id'])
        maximum = int(job['mission'].get('limits', {}).get('max_artifact_bytes', 256 * 1024 * 1024))
        path = engine.settings.state_dir / 'staging' / ('upload-' + uuid.uuid4().hex)
        digest, count = hashlib.sha256(), 0
        try:
            with path.open('xb') as stream:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > maximum:
                        raise AccessDenied('Upload exceeds artifact limit')
                    scope = f"{row['job_id']}:{assignment['revision']}:{assignment['generation']}"
                    limit = job['mission'].get('budget', {}).get('max_bytes', 1_000_000_000)
                    if not store.reserve_budget(scope, 'executor_upload_bytes', len(chunk), limit)['allowed']:
                        raise AccessDenied('Shared executor upload byte budget exhausted')
                    digest.update(chunk)
                    stream.write(chunk)
            context(worker, data)  # Recheck the live task fence after network IO.
            record = row['record']
            if digest.hexdigest() != record['sha256'] or count != record.get('bytes'):
                raise AccessDenied('Uploaded bytes do not match the original receipt')
            resource = next(item for item in store.resources(row['job_id']) if item['id'] == record['resource_id'])
            identity = {**resource, **record}
            expected = {key: identity[key] for key in ('role', 'doi', 'title', 'version') if identity.get(key)}
            verified = await asyncio.to_thread(engine.vault.commit_file, path, expected)
            meta = {key: value for key, value in record.items() if key not in ('path', 'sha256', 'integrity', 'identity', 'status', 'bytes', 'issues', 'identity_evidence')}
            artifact = store.add_artifact(row['job_id'], {**meta, **verified, 'executor_id': worker_id,
                'executor_boot_id': worker['boot_id'], 'upload_receipt_id': upload_id}, lease=task_lease(assignment['task']))
            manager._put('upload', upload_id, {**row, 'status': 'committed', 'artifact': artifact}, row['job_id'])
            return artifact
        finally:
            path.unlink(missing_ok=True)

    @router.post('/workers/{worker_id}/artifacts/{artifact_id}')
    async def artifact_content(worker_id: str, artifact_id: str, request: Request):
        worker = auth(worker_id, request)
        assignment, command = context(worker, await request.json())
        artifact = next((item for item in store.artifacts(assignment['job_id']) if item['id'] == artifact_id), None)
        if not artifact:
            raise AccessDenied('Unknown artifact')
        path = Path(artifact['path']).resolve()
        if not path.is_relative_to((engine.settings.state_dir / 'vault').resolve()):
            raise AccessDenied('Artifact path is outside the coordinator vault')
        return FileResponse(path, media_type=artifact.get('media_type'), headers={'x-ore-sha256': artifact['sha256']})

    return router
