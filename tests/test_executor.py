import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ore.config import SecretStore, Settings
from ore.execution import ExecutionManager, ExecutorUnavailable, create_execution_router
from ore.policy import AccessDenied
from ore.store import LeaseLost, Store
import ore.store as store_module
from ore.vault import Vault


@pytest.fixture
def setup(tmp_path):
    settings = Settings(state_dir=tmp_path / 'state', max_workers=0,
        execution_backend='remote', executor_enrollment_token='fixture-enrollment').prepare()
    store = Store(settings.database_url)
    store.initialize()
    profile = {'id': 'public', 'max_browser_sessions': 4, 'sources': {'scopus': {'api_key_ref': 'allowed-key'}}}
    engine = SimpleNamespace(settings=settings, store=store, secrets=SecretStore(settings.state_dir),
        vault=Vault(settings.state_dir / 'vault'), profile=lambda mission: profile,
        event=lambda job_id, kind, payload: store.append_event(job_id, kind, payload))
    manager = ExecutionManager(engine)
    engine.execution = manager
    app = FastAPI()
    app.include_router(create_execution_router(manager))
    @app.middleware('http')
    async def authenticate(request, call_next):
        if not manager.authorize_request(request):
            return JSONResponse({'detail': 'unauthorized'}, status_code=401)
        return await call_next(request)
    @app.exception_handler(AccessDenied)
    async def denied(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=403)
    @app.exception_handler(LeaseLost)
    async def expired(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)
    yield engine, manager, app
    if manager.monitor:
        manager.monitor.cancel()
    store.close()


async def assigned(engine, manager, name='executor-one', **mission_fields):
    grant = await manager.register('fixture-enrollment', {'id': name, 'boot_id': 'boot-' + name})
    job = engine.store.create_job({'goal': 'Controlled executor test', **mission_fields})
    engine.store.create_task(job['id'], 'retrieve', {}, 'root')
    task = engine.store.claim_task('model-' + name, 120, job['id'])
    assignment = await manager._assignment(task, job['id'])
    command = {'id': 'command-' + name, 'assignment_id': assignment['id'], 'worker_id': name,
        'boot_id': grant['boot_id'], 'job_id': job['id'], 'kind': 'tool',
        'payload': {'name': 'download'}, 'operator': False, 'status': 'running'}
    manager._put('command', command['id'], command, job['id'])
    envelope = {'assignment_id': assignment['id'], 'command_id': command['id'],
        'capability': engine.secrets.get('executor-assignment:' + assignment['id'])}
    return grant, job, task, assignment, envelope


@pytest.mark.asyncio
async def test_explicit_auth_does_not_grant_prefix_or_other_worker_access(setup):
    engine, manager, app = setup
    first = await manager.register('fixture-enrollment', {'id': 'one'})
    await manager.register('fixture-enrollment', {'id': 'two'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + first['token']}) as client:
        assert (await client.post('/v1/execution/workers/one/heartbeat')).status_code == 200
        assert (await client.post('/v1/execution/workers/two/heartbeat')).status_code == 401
        assert (await client.get('/v1/execution/workers/one/heartbeat')).status_code == 401
        assert (await client.post('/v1/execution/admin')).status_code == 401
        assert (await client.post('/v1/secrets/stolen')).status_code == 401
        assert (await client.post('/v1/execution/register', json={})).status_code == 401
    await manager.close()


@pytest.mark.asyncio
async def test_scoped_rpc_rejects_cross_job_and_unreferenced_secrets(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    engine.secrets.set('allowed-key', 'fixture-visible-key')
    engine.secrets.set('not-allowed', 'fixture-hidden-key')
    prefix = '/v1/execution/workers/executor-one/rpc'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        response = await client.post(prefix, json={**envelope, 'method': 'get_job', 'args': ['other-job']})
        assert response.status_code == 403
        response = await client.post(prefix, json={**envelope, 'method': 'secret.get', 'args': ['not-allowed']})
        assert response.status_code == 403 and 'fixture-hidden-key' not in response.text
        response = await client.post(prefix, json={**envelope, 'method': 'secret.get', 'args': ['allowed-key']})
        assert response.json()['result'] == 'fixture-visible-key'
        response = await client.post(prefix, json={**envelope, 'method': 'put_document', 'args': ['coverage', 'forged', {}]})
        assert response.status_code == 403
    await manager.close()


@pytest.mark.asyncio
async def test_assignment_lost_on_executor_boot_replacement(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    worker = manager.authenticate(grant['id'], grant['token'])
    manager.publish_sessions(worker, assignment, [{'id': 'browser-one', 'job_id': job['id'], 'epoch': 1, 'control': 'human'}])
    await manager.register('fixture-enrollment', {'id': grant['id'], 'boot_id': 'replacement'})
    assert manager.get_session('browser-one').closed
    assert manager._get('assignment', assignment['id'])['status'] == 'lost'
    with pytest.raises(AccessDenied):
        manager.authenticate(grant['id'], grant['token'])
    await manager.close()


@pytest.mark.asyncio
async def test_result_rejected_after_task_lease_expiry(setup, monkeypatch):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    future = datetime.now(timezone.utc) + timedelta(seconds=121)
    monkeypatch.setattr(store_module, 'utcnow', lambda: future)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        response = await client.post('/v1/execution/workers/executor-one/result', json={**envelope, 'result': {'ok': True}})
        assert response.status_code == 409
    assert manager._get('command', envelope['command_id'])['status'] == 'running'
    await manager.close()


@pytest.mark.asyncio
async def test_upload_rehashes_original_and_fences_after_stream(setup, monkeypatch):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    engine.store.upsert_resource(job['id'], {'id': 'resource-one', 'url': 'https://example.org/a'})
    content = b'controlled original bytes'
    record = {'resource_id': 'resource-one', 'role': 'attachment', 'bytes': len(content),
        'sha256': hashlib.sha256(content).hexdigest(), 'status': 'verified'}
    prefix = '/v1/execution/workers/executor-one/uploads'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        upload = (await client.post(prefix, json={**envelope, 'record': record})).json()['upload_id']
        response = await client.put(prefix + '/' + upload, content=b'X' * len(content), headers={'x-ore-assignment': envelope['capability']})
        assert response.status_code == 403 and not engine.store.artifacts(job['id'])
        response = await client.put(prefix + '/' + upload, content=content, headers={'x-ore-assignment': envelope['capability']})
        assert response.status_code == 200
        assert response.json()['sha256'] == record['sha256']
        upload2 = (await client.post(prefix, json={**envelope, 'record': record})).json()['upload_id']
        async def body():
            yield content
            future = datetime.now(timezone.utc) + timedelta(seconds=121)
            monkeypatch.setattr(store_module, 'utcnow', lambda: future)
        response = await client.put(prefix + '/' + upload2, content=body(), headers={'x-ore-assignment': envelope['capability']})
        assert response.status_code == 409
        assert len(engine.store.artifacts(job['id'])) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_origin_rate_is_shared_and_cannot_lower_coordinator_interval(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager,
        limits={'origin_min_interval_seconds': 4})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        response = await client.post('/v1/execution/workers/executor-one/rpc', json={**envelope,
            'method': 'acquire_rate_slot', 'args': ['public:example.org', 0]})
        assert response.status_code == 200
        result = response.json()['result']
        assert result['interval'] == 4
        second = engine.store.acquire_rate_slot('executor-origin:example.org', 4)
        assert second['delay_seconds'] >= 3
    await manager.close()


@pytest.mark.asyncio
async def test_new_task_fence_rotates_assignment_and_preserves_released_session(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    worker = manager.authenticate(grant['id'], grant['token'])
    manager.publish_sessions(worker, assignment, [{'id': 'preserved-browser', 'job_id': job['id'], 'epoch': 2, 'control': 'human'}])
    engine.store.fail_task(task['id'], task['worker_id'], task['fence'], {}, task['revision'], state='awaiting_user')
    engine.store.reset_for_resume(job['id'])
    replacement = engine.store.claim_task('replacement-model', 120, job['id'])
    with pytest.raises(ExecutorUnavailable, match='user still controls'):
        await manager._assignment(replacement, job['id'])
    current = manager._get('assignment', assignment['id'])
    manager.publish_sessions(worker, current, [{'id': 'preserved-browser', 'job_id': job['id'], 'epoch': 3, 'control': 'agent'}])
    rebound = await manager._assignment(replacement, job['id'])
    assert rebound['id'] == assignment['id']
    assert rebound['worker_id'] == grant['id']
    assert rebound['task']['fence'] == replacement['fence']
    with pytest.raises(AccessDenied, match='capability'):
        manager.validate_assignment(worker, assignment['id'], envelope['capability'])
    assert manager._get('command', envelope['command_id'])['status'] == 'cancel_requested'
    await manager.close()


@pytest.mark.asyncio
async def test_explicit_source_probe_rechecks_only_selected_readiness_and_keeps_scope(monkeypatch):
    from copy import deepcopy
    import socket
    from ore.executor import source_probe_policy
    from ore.source_policy import authorize_operation
    profile = {'id': 'institution', 'origins': ['https://api.elsevier.com'],
        'sources': {'scopus': {'api_key_ref': 'scoped-key'}},
        'source_readiness': {'scopus': {'search': {'state': 'approval_pending'},
                                      'resolve': {'state': 'entitlement_denied'}}}}
    original = deepcopy(profile)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    policy = source_probe_policy(profile, 'scopus', 'search')
    assert await policy.check('https://api.elsevier.com/content/search/scopus')
    assert profile == original
    assert policy.profile['source_readiness']['scopus']['resolve']['state'] == 'entitlement_denied'
    assert not authorize_operation(policy.mission, policy.profile, 'crossref', 'search')['allowed']
    with pytest.raises(AccessDenied):
        await policy.check('https://outside.example/check')
    excluded = deepcopy(profile)
    excluded['source_policy'] = {'exclude': {'search': ['scopus']}}
    with pytest.raises(AccessDenied, match='source excluded'):
        await source_probe_policy(excluded, 'scopus', 'search').check('https://api.elsevier.com/content/search/scopus')
    with pytest.raises(AccessDenied):
        source_probe_policy(profile, 'scopus', 'download')


@pytest.mark.asyncio
async def test_executor_cannot_increase_first_budget_or_challenge_limits(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager,
        budget={'max_bytes': 3}, limits={'max_browser_actions': 1},
        on_challenge={'max_attempts_per_episode': 1, 'max_active_seconds': 10})
    scope = f"{job['id']}:{job['revision']}:{job['generation']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        async def rpc(method, args):
            return await client.post('/v1/execution/workers/executor-one/rpc',
                json={**envelope, 'method': method, 'args': args})
        first = (await rpc('reserve_budget', [scope, 'download_bytes', 2, 999])).json()['result']
        assert first['allowed'] and first['limit'] == 3
        assert not (await rpc('reserve_budget', [scope, 'download_bytes', 2, 999])).json()['result']['allowed']
        assert (await rpc('reserve_budget', [scope, 'model_tokens', 1, 999])).status_code == 403
        assert (await rpc('reserve_budget', [scope, 'browser_actions', 0, 999])).status_code == 403
        attempt = (await rpc('reserve_challenge', [job['id'], 'https://example.org', 'public:operator', 99, 999])).json()['result']
        assert attempt['allowed'] and attempt['max_attempts'] == 1 and attempt['max_active_seconds'] == 10
        await rpc('finish_challenge', [attempt['id'], attempt['token']])
        repeat = (await rpc('reserve_challenge', [job['id'], 'https://example.org', 'public:operator', 99, 999])).json()['result']
        assert not repeat['allowed'] and repeat['reason'] == 'budget_exhausted'
    await manager.close()


@pytest.mark.asyncio
async def test_lost_executor_recreated_handoff_is_adopted_by_original_task(setup, monkeypatch):
    engine, manager, app = setup
    first, job, task, assignment, envelope = await assigned(engine, manager)
    old_worker = manager.authenticate(first['id'], first['token'])
    manager.publish_sessions(old_worker, assignment, [{'id': 'lost-browser', 'job_id': job['id'],
        'epoch': 2, 'control': 'human', 'agent_id': 'task:' + task['id']}])
    engine.store.fail_task(task['id'], task['worker_id'], task['fence'], {}, task['revision'], state='awaiting_user')
    await manager.worker_lost(first['id'], 'fixture_process_loss')
    replacement_worker = await manager.register('fixture-enrollment', {'id': 'replacement-executor'})
    async def create_browser(assigned_row, kind, payload, **options):
        assert kind == 'create_session' and payload['agent_id'] == 'task:' + task['id']
        summary = {'id': 'recreated-browser', 'job_id': job['id'], 'epoch': 2, 'control': 'human',
            'agent_id': payload['agent_id']}
        worker = manager.authenticate(replacement_worker['id'], replacement_worker['token'])
        manager.publish_sessions(worker, assigned_row, [summary])
        return summary
    monkeypatch.setattr(manager, '_command', create_browser)
    created = await manager.create_session(job['id'], task_id=task['id'])
    restored = manager._get('assignment', manager.get_session(created['id']).assignment_id)
    assert restored['task'] is None and restored['recovery_task_id'] == task['id']
    assert [item['id'] for item in await manager.task_sessions(job['id'], task['id'])] == [created['id']]
    assert await manager.task_sessions(job['id'], 'unrelated-task') == []
    assert [item['id'] for item in await manager.task_sessions(job['id'])] == [created['id']]
    old_capability = engine.secrets.get('executor-assignment:' + restored['id'])
    with pytest.raises(LeaseLost, match='no active task lease'):
        manager.validate_assignment(manager.authenticate(replacement_worker['id'], replacement_worker['token']), restored['id'], old_capability)
    assert manager.get_session('lost-browser').closed
    engine.store.reset_for_resume(job['id'])
    claim = engine.store.claim_task('resumed-model', 120, job['id'])
    with pytest.raises(ExecutorUnavailable, match='user still controls'):
        await manager._assignment(claim, job['id'])
    worker = manager.authenticate(replacement_worker['id'], replacement_worker['token'])
    manager.publish_sessions(worker, restored, [{**created, 'epoch': 3, 'control': 'agent'}])
    adopted = await manager._assignment(claim, job['id'])
    assert adopted['id'] == restored['id'] and adopted['task']['id'] == task['id']
    assert adopted['task']['fence'] > task['fence'] and not adopted['operator']
    assert manager.get_session(created['id']).metadata['agent_id'] == 'task:' + claim['id']
    with pytest.raises(AccessDenied, match='capability'):
        manager.validate_assignment(worker, adopted['id'], old_capability)
    with pytest.raises(AccessDenied):
        await manager.create_session(job['id'], task_id=claim['id'], agent_id='task:other')
    another = engine.store.create_job({'goal': 'Another job'})
    with pytest.raises(AccessDenied):
        await manager.create_session(another['id'], task_id=claim['id'])
    await manager.close()


@pytest.mark.asyncio
async def test_stale_finally_cannot_close_recovery_or_newer_attempt(setup, monkeypatch):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    engine.store.fail_task(task['id'], task['worker_id'], task['fence'], {}, task['revision'], state='awaiting_user')
    await manager.worker_lost(grant['id'], 'fixture_process_loss')
    await manager.register('fixture-enrollment', {'id': 'recovery-executor'})
    recovery = await manager._assignment(None, job['id'], operator=True, recovery_task_id=task['id'])
    released = []
    async def release(row):
        released.append(row['id'])
    monkeypatch.setattr(manager, '_release', release)
    # No human-control shortcut: a replacement not yet taken over is also protected.
    await manager.release_task(task)
    assert released == [] and recovery['task'] is None
    engine.store.reset_for_resume(job['id'])
    newer = engine.store.claim_task('newer-model', 120, job['id'])
    adopted = await manager._assignment(newer, job['id'])
    assert adopted['id'] == recovery['id']
    await manager.release_task(task)
    assert released == []
    await manager.release_task(newer)
    assert released == [adopted['id']]
    await manager.close()


@pytest.mark.asyncio
async def test_remote_resource_lane_has_own_floor_and_shared_origin_cooldown(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager,
        limits={'origin_min_interval_seconds': 3, 'browser_resource_min_interval_seconds': .15})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        async def rpc(method, args, **kwargs):
            return await client.post('/v1/execution/workers/executor-one/rpc',
                json={**envelope, 'method': method, 'args': args, 'kwargs': kwargs})
        main = (await rpc('acquire_rate_slot', ['public:example.org', 0])).json()['result']
        resource = (await rpc('acquire_rate_slot', ['public:example.org', 0], lane='browser_resource')).json()['result']
        assert main['interval'] == 3 and resource['interval'] == .15
        assert resource['delay_seconds'] < .1
        await rpc('penalize_rate', ['public:example.org', 1])
        delayed = (await rpc('confirm_rate_slot', ['public:example.org', resource['slot_at']], lane='browser_resource')).json()['result']
        assert not delayed['allowed'] and delayed['delay_seconds'] > .8
        assert (await rpc('acquire_rate_slot', ['public:example.org', 0], lane='unbounded')).status_code == 403
    await manager.close()

@pytest.mark.asyncio
async def test_cancel_ack_survives_revoked_lease_without_accepting_result(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    command=manager._get('command', envelope['command_id'])
    manager._put('command', command['id'], {**command,'status':'cancel_requested'},job['id'])
    engine.store.update_job(job['id'],status='paused')
    prefix='/v1/execution/workers/executor-one'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture',headers={'authorization':'Bearer '+grant['token']}) as client:
        assert (await client.post(prefix+'/result',json={**envelope,'result':{'must_not_commit':True}})).status_code==409
        bad=await client.post(prefix+'/cancelled',json={**envelope,'capability':'wrong'})
        assert bad.status_code==403
        receipt=await client.post(prefix+'/cancelled',json=envelope)
        assert receipt.status_code==200 and receipt.json()['status']=='acknowledged'
        row=manager._get('command',envelope['command_id'])
        assert row['status']=='cancelled' and row['cancel_acknowledged_at'] and 'result' not in row
        assert (await client.post(prefix+'/cancelled',json=envelope)).json()['status']=='duplicate'
    await manager.close()

@pytest.mark.asyncio
async def test_active_command_cannot_ack_nonexistent_interrupt(setup):
    engine,manager,app=setup
    grant,job,task,assignment,envelope=await assigned(engine,manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture',headers={'authorization':'Bearer '+grant['token']}) as client:
        assert (await client.post('/v1/execution/workers/executor-one/cancelled',json=envelope)).status_code==409
    await manager.close()

@pytest.mark.asyncio
async def test_terminal_receipt_is_idempotent_without_changing_result(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    command = manager._get('command', envelope['command_id'])
    expected = {**command, 'status': 'failed', 'error': {'code': 'fixture_failure'}}
    manager._put('command', command['id'], expected, job['id'])
    before = manager._get('command', command['id'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        response = await client.post('/v1/execution/workers/executor-one/cancelled', json=envelope)
        assert response.status_code == 200 and response.json()['status'] == 'settled'
        assert manager._get('command', command['id']) == before
        denied = await client.post('/v1/execution/workers/executor-one/cancelled',
                                  json={**envelope, 'capability': 'wrong'})
        assert denied.status_code == 403
    await manager.close()


@pytest.mark.asyncio
async def test_receipt_outbox_retries_lost_result_and_cancel_ack_on_reconnect(tmp_path):
    import json
    from ore.executor import ReceiptOutbox
    report = {'assignment_id': 'a', 'capability': 'private-assignment-capability', 'command_id': 'command',
              'result': {'uncommitted': True}}
    box = ReceiptOutbox(tmp_path, 'worker', 'same-boot')
    box.enqueue(report)
    path = next(box.path.glob('*.json'))
    assert path.stat().st_mode & 0o777 == 0o600
    calls = []
    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError('fixture disconnection')
        if request.url.path.endswith('/result'):
            return httpx.Response(409)
        if len(calls) == 3:
            return httpx.Response(503)
        return httpx.Response(200, json={'status': 'acknowledged', 'command_id': 'command'})
    async with httpx.AsyncClient(base_url='http://fixture', transport=httpx.MockTransport(transport)) as client:
        assert not await box.flush(client, '/worker')
        restored = ReceiptOutbox(tmp_path, 'worker', 'same-boot')
        assert not await restored.flush(client, '/worker')
        persisted = json.loads(path.read_text())
        assert persisted['phase'] == 'cancelled' and 'report' not in persisted
        assert 'uncommitted' not in path.read_text()
        assert await restored.flush(client, '/worker')
        assert not path.exists()
    assert [request.url.path for request in calls] == [
        '/worker/result', '/worker/result', '/worker/cancelled', '/worker/cancelled']
    assert all('result' not in json.loads(request.content) for request in calls if request.url.path.endswith('/cancelled'))
    assert not list(ReceiptOutbox(tmp_path, 'worker', 'new-boot').path.glob('*.json'))


@pytest.mark.asyncio
async def test_receipt_outbox_checks_success_body_and_retains_unconfirmed_ack(tmp_path):
    from ore.executor import ReceiptOutbox
    box = ReceiptOutbox(tmp_path, 'worker', 'boot')
    box.enqueue({'assignment_id': 'a', 'capability': 'fixture', 'command_id': 'command',
                 'error': {'code': 'interrupted'}})
    def transport(request):
        return httpx.Response(409) if request.url.path.endswith('/result') else httpx.Response(
            200, json={'status': 'acknowledged', 'command_id': 'different-command'})
    async with httpx.AsyncClient(base_url='http://fixture', transport=httpx.MockTransport(transport)) as client:
        assert not await box.flush(client, '/worker')
        assert list(box.path.glob('*.json'))


@pytest.mark.asyncio
async def test_control_heartbeat_detects_cancel_while_event_loop_is_blocked():
    import threading
    import time
    from ore.executor import ExecutorHeartbeat
    detected = threading.Event()
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={'cancel_commands': ['active-command']})
    def cancel(commands):
        if 'active-command' in commands:
            detected.set()
    client = httpx.Client(base_url='http://fixture', transport=httpx.MockTransport(transport))
    heartbeat = ExecutorHeartbeat('http://fixture', 'fixture', '/worker', cancel, interval=.01, client=client)
    heartbeat.start()
    try:
        # Deliberately emulate an existing synchronous ScopedStore socket wait.
        time.sleep(.08)
        assert detected.is_set()
        assert calls and calls[0].url.path == '/worker/heartbeat'
    finally:
        await heartbeat.close()
    assert not heartbeat.thread.is_alive()


def executor_context(tmp_path):
    from ore.executor import ExecutionContext
    assignment = {'id': 'assignment', 'capability': 'fixture-capability', 'job_id': 'job',
                  'profile': {'id': 'public'}, 'job': {'mission': {'goal': 'Fixture'}}, 'task': None}
    return ExecutionContext('http://fixture', 'worker', 'fixture-token', assignment, tmp_path)


@pytest.mark.asyncio
async def test_upload_checks_cancel_between_chunks_and_does_not_accept_artifact(tmp_path):
    from ore.executor import ScopedStore
    context = executor_context(tmp_path)
    context.client.close()
    received = []
    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            if request.method == 'POST':
                return httpx.Response(200, json={'upload_id': 'upload'})
            chunks = iter(request.stream)
            received.append(next(chunks))
            context.request_cancel()
            next(chunks)  # Must fail before the second chunk can leave the worker.
            raise AssertionError('Cancelled upload continued')
    context.client = httpx.Client(base_url='http://fixture', transport=Transport())
    path = context.state_dir/'staging'/'large'
    path.write_bytes(b'x'*(128*1024))
    try:
        with pytest.raises(LeaseLost, match='interrupted'):
            ScopedStore(context).add_artifact('job', {'path': str(path), 'bytes': path.stat().st_size})
        assert [len(chunk) for chunk in received] == [64*1024]
        assert context.local_artifacts == {}
        assert context._io_active == 0
    finally:
        await context.close()


@pytest.mark.asyncio
async def test_cancelled_sync_work_settles_before_command_can_acknowledge(tmp_path):
    import asyncio
    import threading
    context = executor_context(tmp_path)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def blocked():
        with context.io_operation():
            entered.set()
            try:
                release.wait(2)
            finally:
                exited.set()
    task = asyncio.create_task(context.sync_operation(blocked))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(.02)
        assert context.cancel_event.is_set()
        assert not task.done() and not exited.is_set()
        settlement = asyncio.create_task(context.wait_io_settled())
        await asyncio.sleep(.02)
        assert not settlement.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settlement
        assert exited.is_set() and context._io_active == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await context.close()
