"""Coordinator API for authenticated host workers; credentials stay on their hosts.

Mount with ``app.include_router(create_worker_router(engine))`` behind the same
/v1 authentication middleware as the rest of the coordinator API.
"""
from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, Request

from .policy import AccessDenied, redact
from .store import LeaseLost
from .tools import TOOLS, ToolRuntime

ACTIVE = ('queued', 'running', 'resuming')
PAUSED = ('paused', 'awaiting_user', 'awaiting_auth')


def create_worker_router(engine):
    router = APIRouter(prefix='/v1/workers')
    runtimes = {}

    def remember(value):
        saved = engine.store.register_worker(value)
        engine.workers[saved['id']] = saved
        return saved

    def registered(ident):
        value = engine.store.get_worker(ident)
        if value is None:
            raise HTTPException(404, 'Unknown worker; register first')
        return value

    def runtime_for(task):
        key = (task['id'], task['fence'], task['revision'], task['generation'])
        # A recovered lease must never reuse an old ToolRuntime's fencing token.
        for old in list(runtimes):
            if old[0] == task['id'] and old != key:
                runtimes.pop(old, None)
        if key not in runtimes:
            runtimes[key] = ToolRuntime(engine, task['job_id'], task)
        return runtimes[key]

    def release_runtime(task):
        for key in list(runtimes):
            if key[0] == task['id']:
                runtimes.pop(key, None)

    @router.get('')
    async def workers():
        saved = {value['id']: value for value in engine.store.list_workers()}
        saved.update(engine.workers)
        return list(saved.values())

    @router.post('/register')
    async def register(request: Request):
        value = await request.json()
        ident = value.get('id') or uuid.uuid4().hex
        if not isinstance(ident, str) or not 1 <= len(ident) <= 200:
            raise HTTPException(422, 'Worker id must be 1 to 200 characters')
        models = value.get('models', [])
        if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
            raise HTTPException(422, 'Worker models must be a model catalog array')
        previous = engine.store.get_worker(ident)
        active = engine.store.get_task(previous.get('task_id')) if previous and previous.get('task_id') else None
        running = bool(active and active['state'] == 'running' and active['worker_id'] == ident)
        remember({'id': ident, 'kind': 'remote_agent', 'backend_kind': 'codex',
                  'state': 'running' if running else 'idle', 'task_id': active['id'] if running else None,
                  'models': models})
        return {'id': ident}

    @router.post('/{worker}/claim')
    async def claim(worker: str, request: Request):
        raw = await request.body()
        data = json.loads(raw) if raw else {}
        job_ids = data.get('job_ids')
        if job_ids is not None and (not isinstance(job_ids, list) or not all(isinstance(item, str) for item in job_ids)):
            raise HTTPException(422, 'job_ids must be an array of job IDs or null')
        host = registered(worker)
        if host.get('task_id'):
            current = engine.store.get_task(host['task_id'])
            if current and current['state'] == 'running' and current['worker_id'] == worker:
                # A caller must settle its existing attempt before taking another.
                try:
                    engine.store.validate_task_lease(current['id'], worker, current['fence'], current['revision'])
                except LeaseLost:
                    pass
                else:
                    return {'task': None, 'reason': 'worker_already_running'}
        unavailable = []
        for job in engine.store.list_jobs():
            if job_ids is not None and job['id'] not in job_ids:
                continue
            if job['status'] not in ACTIVE:
                continue
            backend = job['mission'].get('backend', 'codex')
            backend = backend.get('kind', 'codex') if isinstance(backend, dict) else backend
            if backend != 'codex':
                continue
            try:
                engine.check_egress(job['mission'], backend_kind='codex')
                eligible = []
                for kind in {task['kind'] for task in engine.store.tasks(job['id'])
                             if task['revision'] == job['revision'] and task['generation'] == job['generation']}:
                    if kind == 'workflow':continue  # Workflow scheduler owns graph/epoch semantics.
                    try:
                        engine.routing_for(job, kind, catalog=host.get('models', []))
                    except (AccessDenied, ValueError):
                        continue
                    eligible.append(kind)
                if not eligible:
                    unavailable.append({'job_id': job['id'], 'reason': 'no_supported_model_route'})
                    continue
            except (AccessDenied, ValueError) as exc:
                unavailable.append({'job_id': job['id'], 'reason': type(exc).__name__})
                continue
            task = await asyncio.to_thread(engine.store.claim_task, worker, 120, job['id'], eligible, ('codex',))
            if task is None:
                continue
            # Mission revisions may change between admission checks and claim.
            job = engine.store.get_job(task['job_id'])
            try:
                engine.check_egress(job['mission'], backend_kind='codex')
                engine.routing_for(job, task['kind'], catalog=host.get('models', []))
                runtime = runtime_for(task)
                state = await runtime.execute('state', {})
                offer = engine.model_observation(job['mission'], {'task': task, 'job': job, 'state': state, 'tools': TOOLS})
            except Exception:
                engine.store.fail_task(task['id'], worker, task['fence'], {'code': 'worker_offer_unavailable'},
                                       task['revision'], retry_seconds=2)
                release_runtime(task)
                raise
            remember({'id': worker, 'state': 'running', 'task_id': task['id']})
            return offer
        remember({'id': worker, 'state': 'idle', 'task_id': None, 'unavailable': unavailable})
        return {'task': None, 'unavailable': unavailable}

    @router.post('/{worker}/tasks/{task_id}/{action}')
    async def worker_action(worker: str, task_id: str, action: str, request: Request):
        host = registered(worker)
        data = await request.json()
        try:
            fence = int(data['fence'])
        except (KeyError, ValueError, TypeError):
            raise HTTPException(422, 'A valid fence is required')
        task = engine.store.validate_task_lease(task_id, worker, fence)
        job = engine.store.get_job(task['job_id'])
        # Paused attempts can settle, but cannot renew or perform more actions.
        if action == 'fail':
            state = job['status'] if job['status'] in PAUSED else 'cancelled' if job['status'] == 'cancelled' else 'awaiting_user' if data.get('state') == 'awaiting_user' else 'blocked'
            result = engine.store.fail_task(task_id, worker, fence, redact(data.get('error', {})), task['revision'], state=state)
            release_runtime(task)
            execution = getattr(engine, 'execution', None)
            if execution and execution.enabled:
                await execution.release_task(task)
            remember({'id': worker, 'state': 'idle', 'task_id': None})
            engine.reconcile(job['id'])
            return result
        if job['status'] not in ACTIVE:
            raise HTTPException(409, {'code': 'job_paused', 'status': job['status']})
        if action not in ('heartbeat', 'route', 'decision', 'tool', 'finish'):
            raise HTTPException(404, 'Unknown worker action')
        engine.store.heartbeat(task_id, worker, fence, 120, allowed_job_states=ACTIVE)
        remember({'id': worker})
        if action == 'heartbeat':
            return {'status': 'ok'}
        engine.check_egress(job['mission'], backend_kind='codex')
        if action == 'route':
            route = engine.routing_for(job, task['kind'], data.get('failures', 0), catalog=host.get('models', []))
            allocation = engine.store.reserve_budget(f"{job['id']}:{job['revision']}:{job['generation']}",
                'agent_turns', 1, job['mission'].get('budget', {}).get('max_turns', 100))
            if not allocation['allowed']:
                raise AccessDenied('Shared turn budget exhausted')
            engine.event(job['id'], 'model_selected', {'task_id': task_id, 'worker_id': worker, **route})
            return route
        if action == 'decision':
            usage = data.get('usage', {})
            token_count = usage.get('totalTokens', usage.get('total_tokens', 0))
            if not isinstance(token_count, int) or token_count < 0:
                raise HTTPException(422, 'Observed token count must be a nonnegative integer')
            budget = job['mission'].get('budget', {})
            if budget.get('max_tokens') is not None:
                allocation = engine.store.reserve_budget(f"{job['id']}:{job['revision']}:{job['generation']}",
                    'model_tokens', token_count, budget['max_tokens'])
                if not allocation['allowed']:
                    raise AccessDenied('Observed model token budget exceeded; no more actions will run')
            engine.event(job['id'], 'agent_decision', {'task_id': task_id, 'worker_id': worker,
                'thread_id': data.get('thread_id'), 'step': data.get('step'),
                'decision': data.get('decision', {}), 'usage': usage})
            return {'status': 'recorded'}
        if action == 'tool':
            runtime = runtime_for(task)
            result = await runtime.execute(data['tool'], data.get('arguments', {}))
            return engine.model_observation(job['mission'], {'result': result, 'image_url': runtime.last_image})
        result = engine.store.finish_task(task_id, worker, fence, data.get('result', {}), task['revision'])
        release_runtime(task)
        execution = getattr(engine, 'execution', None)
        if execution and execution.enabled:
            await execution.release_task(task)
        remember({'id': worker, 'state': 'idle', 'task_id': None})
        engine.reconcile(job['id'])
        return result

    return router
