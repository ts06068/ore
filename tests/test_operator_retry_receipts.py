"""A failed workflow resume reuses the operator grant; it never renews its clock."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore import challenge_policy
from ore.desktop import DesktopError
from ore.operator_retry import authorize_retry
from test_challenge_adaptation import clock
from test_engine import engine
from test_server import client, login
from test_workflow_native import plan, agent


@pytest.fixture
async def receipt_case(engine, client, clock, monkeypatch):
    await login(client)
    run = engine.workflows.create_run(plan([agent()], mission={'artifact_roles': [],
        'allowed_origins': ['https://fixture.test'], 'on_challenge': challenge_policy.default_policy()}))
    await engine.workflows.start_run(run['id'])
    task = engine.store.claim_task('fixture-worker', job_id=run['job_id'])
    assert task
    engine.store.fail_task(task['id'], task['worker_id'], task['fence'], {'code': 'fixture_wait'},
                           revision=task['revision'], state='awaiting_user')
    with engine.store._tx() as conn:
        job = engine.store._job(conn, run['job_id'])
        node = engine.store._doc(conn, 'workflow.node', [run['id'], 'parent'])['data']
        node.update(status='awaiting_user', continuation_delta={'session_id': 'retry-browser'})
        engine.workflows._save(conn, 'workflow.node', [run['id'], 'parent'], node, job)
        current_run = engine.store._doc(conn, 'workflow.run', run['id'])['data']
        current_run['status'] = 'awaiting_user'
        engine.workflows._save(conn, 'workflow.run', run['id'], current_run, job)
    engine.store.update_job(job['id'], status='awaiting_user')
    job = engine.store.get_job(job['id']);profile = engine.profile(job['mission'])
    session = SimpleNamespace(id='retry-browser', job_id=job['id'], profile_id=profile.get('id', 'public'),
        principal_id=profile.get('principal_id', 'operator'), agent_id='task:' + task['id'], epoch=10,
        control='agent', mission=deepcopy(job['mission']), policy=SimpleNamespace(profile=deepcopy(profile)),
        context=SimpleNamespace(desktop=False), closed=False)
    engine.browser = SimpleNamespace(get=lambda sid: session if sid == session.id else None,
        summary=AsyncMock(return_value={'epoch': 10, 'url': 'https://fixture.test/issue', 'transport': 'desktop_chrome'}),
        close=AsyncMock())
    episode = engine.store.observe_challenge(job['id'], 'https://fixture.test', 'public:operator')
    clock[0] += timedelta(seconds=301);engine.store.adapt_challenge(episode['id'])
    prior = deepcopy(engine.store.get_challenge(episode['id']))
    row = engine.handoffs.create(job['id'], 'challenge', 'Expired fixture', session_id=session.id, task_id=task['id'],
        context={'url': 'https://fixture.test/issue', 'challenge_id': episode['id'], 'epoch': session.epoch})
    async def grant_once(engine, broker, current, body):
        grant = authorize_retry(engine.store, current, key=body['idempotency_key'])
        return {'status': 'resolved', 'resolution': 'operator_retry_authorized', 'resume_pending': True,
            'session_id': session.id, 'session_state': 'live', 'control_epoch': session.epoch,
            'retry_allocation': {'deadline_at': grant['deadline_at'], 'previous_episode_ref': grant['previous_episode_ref']}}
    authorization = AsyncMock(side_effect=grant_once)
    monkeypatch.setattr('ore.operator_retry.retry_verification', authorization)
    view = (await client.get('/v1/handoffs/' + row['id'])).json()
    request = {'action': 'retry_verification', 'expected_version': view['state_version'],
               'expected_epoch': 10, 'idempotency_key': 'one-operator-retry-receipt'}
    return SimpleNamespace(row=row, request=request, job=job, run=run, prior=prior, episode=episode,
                           session=session, authorization=authorization)


@pytest.mark.parametrize('failure_stage', ['before_schedule', 'after_schedule'])
async def test_same_key_recovers_scheduler_failure_without_new_grant(client, engine, clock, receipt_case, monkeypatch, failure_stage):
    case = receipt_case;original = engine.run
    if failure_stage == 'before_schedule':
        calls = 0
        async def fail_once(job_id):
            nonlocal calls
            calls += 1
            if calls == 1:raise DesktopError('Fixture scheduler unavailable')
            return await original(job_id)
        resume = AsyncMock(side_effect=fail_once);monkeypatch.setattr(engine, 'run', resume)
    else:
        engine.start = AsyncMock(side_effect=[DesktopError('Fixture worker startup unavailable'), None])
    first = await client.post('/v1/handoffs/' + case.row['id'] + '/actions', json=case.request)
    assert first.status_code == 503
    pending = engine.handoffs.get(case.row['id']);assert pending['resume_pending'] is True
    assert pending.get('execution_resumed') is not True
    grant = deepcopy(engine.store.get_challenge(case.episode['id']))
    scheduled = [item['id'] for item in engine.store.tasks(case.job['id']) if item['id'] != case.row['task_id']]
    clock[0] += timedelta(seconds=7)
    second = await client.post('/v1/handoffs/' + case.row['id'] + '/actions', json=case.request)
    assert second.status_code == 200, second.text
    assert second.json()['resume_pending'] is False and second.json()['execution_resumed'] is True
    if failure_stage == 'after_schedule':
        assert engine.start.await_count == 2
        assert [item['id'] for item in engine.store.tasks(case.job['id']) if item['id'] != case.row['task_id']] == scheduled
    final = engine.store.get_challenge(case.episode['id'])
    assert final['deadline_at'] == grant['deadline_at'] and final['episode'] == grant['episode']
    assert final['attempts'] == 0 and case.authorization.await_count == 1
    assert engine.store.get_document('challenge.episode', [case.episode['id'], case.prior['episode']])['snapshot'] == case.prior
    tasks_before = deepcopy(engine.store.tasks(case.job['id']))
    replay = await client.post('/v1/handoffs/' + case.row['id'] + '/actions', json=case.request)
    assert replay.status_code == 200
    assert engine.store.tasks(case.job['id']) == tasks_before


@pytest.mark.parametrize('change', ['expired', 'profile_changed', 'operator_took_control', 'new_key'])
async def test_pending_receipt_does_not_resume_after_authority_or_budget_changes(client, engine, clock, receipt_case, monkeypatch, change):
    case = receipt_case;resume = AsyncMock(side_effect=DesktopError('Fixture scheduler unavailable'))
    monkeypatch.setattr(engine, 'run', resume)
    assert (await client.post('/v1/handoffs/' + case.row['id'] + '/actions', json=case.request)).status_code == 503
    grant = deepcopy(engine.store.get_challenge(case.episode['id']))
    request = dict(case.request)
    if change == 'expired':clock[0] += timedelta(seconds=121)
    elif change == 'profile_changed':
        profile = deepcopy(engine.profile(case.job['mission']));profile['principal_id'] = 'different-operator';engine.save_profile(profile)
    elif change == 'operator_took_control':case.session.control = 'human'
    else:request['idempotency_key'] = 'different-operator-retry-key'
    denied = await client.post('/v1/handoffs/' + case.row['id'] + '/actions', json=request)
    assert denied.status_code in {403, 409}, denied.text
    assert resume.await_count == 1 and case.authorization.await_count == 1
    assert engine.handoffs.get(case.row['id'])['resume_pending'] is True
    final = engine.store.get_challenge(case.episode['id'])
    assert final['episode'] == grant['episode'] and final['deadline_at'] == grant['deadline_at']
