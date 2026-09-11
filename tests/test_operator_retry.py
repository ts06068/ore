"""Operator-only bounded reauthorization preserves every prior challenge episode."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore import challenge_policy
from ore.browser import BrowserManager
from ore.handoffs import BrowserAccess
from ore.models import canonical_digest
from ore.operator_retry import authorize_retry, retry_metadata
from ore.policy import RateLimiter
from ore.store import ControlConflict
from test_challenge_adaptation import setup, clock, detected
from test_browser import site, browser_dependencies
from test_engine import engine
from test_server import client, login
from test_workflow_native import ScriptedSession, plan, agent, drain
from test_desktop_integration import InteractiveDesktop


def expired_request(store, job, clock):
    original = detected(store, job)
    clock[0] += timedelta(seconds=301)
    store.adapt_challenge(original['id'])
    return {'id': 'operator-request', 'job_id': job['id'], 'challenge_id': original['id'], 'task_id': 'waiting-task'}, store.get_challenge(original['id'])


def test_operator_retry_archives_history_and_has_one_nonrenewable_allocation(setup, clock):
    store, job = setup
    row, old = expired_request(store, job, clock)
    old = deepcopy(old)
    grant = authorize_retry(store, row, key='operator-action-one')
    archived = store.get_document('challenge.episode', [old['id'], old['episode']])
    assert archived['snapshot'] == old and archived['snapshot_digest'] == canonical_digest(old)
    assert grant['episode'] == old['episode'] + 1
    assert grant['max_attempts'] == grant['hard_max_attempts'] == 1
    assert grant['max_elapsed_seconds'] == grant['hard_max_elapsed_seconds'] == 120
    assert grant['adaptive'] is False and grant['remaining_seconds'] == 120
    again = authorize_retry(store, row, key='operator-action-one')
    assert again['deadline_at'] == grant['deadline_at']
    with pytest.raises(ControlConflict, match='cannot be renewed'):
        authorize_retry(store, row, key='operator-action-two')
    reserved = store.reserve_challenge(job['id'], grant['origin'], grant['auth_context'])
    assert reserved['allowed'] and reserved['attempts'] == 1
    clock[0] += timedelta(seconds=1)
    store.finish_challenge(grant['id'], reserved['token'], evidence={'reason': 'not_recovered'})
    assert not store.reserve_challenge(job['id'], grant['origin'], grant['auth_context'])['allowed']
    assert store.get_document('challenge.episode', [old['id'], old['episode']])['snapshot'] == old


def test_operator_retry_cannot_be_consumed_or_renewed_by_another_job(setup, clock):
    store, job = setup
    row, old = expired_request(store, job, clock)
    grant = authorize_retry(store, row, key='only-this-execution')
    other = store.create_job({'goal': 'Different task', 'on_challenge': challenge_policy.default_policy()})
    observed = store.observe_challenge(other['id'], grant['origin'], grant['auth_context'])
    assert observed['state'] == 'awaiting_user'
    denied = store.reserve_challenge(other['id'], grant['origin'], grant['auth_context'])
    assert not denied['allowed'] and denied['reason'] == 'operator_retry_owned_by_another_job'
    assert store.get_challenge(grant['id'])['attempts'] == 0
    assert store.get_challenge(grant['id'])['deadline_at'] == grant['deadline_at']


def test_operator_retry_requires_expiry_and_no_inflight_reservation(setup, clock):
    store, job = setup
    value = detected(store, job)
    row = {'id': 'current-request', 'job_id': job['id'], 'challenge_id': value['id'], 'task_id': 'waiting'}
    with pytest.raises(ControlConflict, match='expired'):
        authorize_retry(store, row, key='too-early')
    attempt = store.reserve_challenge(job['id'], value['origin'], value['auth_context'])
    clock[0] += timedelta(seconds=301)
    with pytest.raises(ControlConflict, match='inactive'):
        authorize_retry(store, row, key='still-reserved')
    assert store.get_challenge(value['id'])['token'] == attempt['token']
    assert store.list_documents('challenge.episode') == []


@pytest.mark.browser
@pytest.mark.parametrize('lost_native,passive_recovery', [(False, False), (True, False), (True, True)])
async def test_operator_retry_resumes_same_native_node_without_claiming_access_success(client, engine, site, browser_dependencies, monkeypatch, clock, lost_native, passive_recovery):
    await login(client)
    monkeypatch.setattr('ore.workflow_native.AgentSession', ScriptedSession)
    engine.backend = SimpleNamespace(sessions=[], prompts=[], rounds={}, results=[], usage_totals={}, timeouts=[], close=AsyncMock())
    engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'fixture', 'effort': 'high', 'mode': 'fixed'}
    engine.start = AsyncMock()
    engine.save_profile({'id': 'public', 'allow_private_network': True, 'sources': {},
        **({'desktop_success_text': ['Independent target article']} if lost_native else {})})
    if lost_native:
        class StoppedDesktop(InteractiveDesktop):
            def __init__(self):
                super().__init__()
                self.sessions = {}
            async def create_session(self, sid, **kwargs):
                value = await super().create_session(sid, **kwargs)
                value.closed = False
                self.sessions[sid] = value
                return value
            async def close_session(self, sid):
                await super().close_session(sid)
                self.sessions[sid].closed = True
        runtime = StoppedDesktop()
        monkeypatch.setattr('ore.desktop.DesktopRuntime', lambda *args, **kwargs: runtime)
    engine.browser = BrowserManager(engine.settings, RateLimiter(), store=engine.store, secrets=engine.secrets, on_event=engine.event)
    seen = {}
    async def script(session, prompt):
        if engine.backend.rounds['parent'] == 1:
            opened = await session.call('browser_open', {'url': site + '/challenge',
                **({'transport': 'desktop_chrome'} if lost_native else {})})
            seen['sid'] = opened['session_id']
            await session.call('handoff', {'session_id': seen['sid'], 'reason': 'Expired verification fixture'})
        else:
            state = await session.call('state', {})
            if lost_native:
                assert len(state['browsers']) == 1 and state['browsers'][0]['id'] != seen['sid']
                seen['sid'] = state['browsers'][0]['id']
            else:
                assert [item['id'] for item in state['browsers']] == [seen['sid']]
            delta = engine.workflows.nodes(run['id'])[0]['continuation_delta']
            assert delta['session_id'] == seen['sid'] and delta['reason'] == 'operator_retry_authorized'
            browser = engine.browser.get(seen['sid'])
            if passive_recovery:
                # Native verification completes asynchronously after the operator
                # grant but before the first ordinary agent observation.
                runtime.value.update(title='Independent target',
                    text='Independent target article now visible with substantive study results.')
                observed = await session.call('browser_observe', {'session_id': browser.id})
                assert observed['challenge_recovery']['resolved']
            else:
                reserved = await session.call('challenge', {'session_id': seen['sid']})
                assert reserved['allowed']
                observed = await session.call('browser_action', {'session_id': browser.id, 'epoch': browser.epoch,
                    'action': 'click', **({'x': 200, 'y': 250} if lost_native else {'selector': '#solve'})})
            assert not observed['challenge_detected']
            await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()], mission={'artifact_roles': [], 'allowed_origins': [site],
        'on_challenge': challenge_policy.default_policy(), 'limits': {'origin_min_interval_seconds': 0}}))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'awaiting_user'
    browser = engine.browser.get(seen['sid'])
    clock[0] += timedelta(seconds=301)
    engine.store.adapt_challenge(browser.challenge_id)
    row = engine.handoffs.list(run['job_id'], 'active')[0]
    old = deepcopy(engine.store.get_challenge(browser.challenge_id))
    view = (await client.get('/v1/handoffs/' + row['id'])).json()
    assert view['automatic_retry']['eligible'] and view['automatic_retry']['max_seconds'] == 120
    if lost_native:
        # Native watchdog ended the container while the broker facade remains live.
        runtime.sessions[browser.id].closed = True
        assert not browser.closed and BrowserAccess(engine).exists(browser.id)
    request = {'action': 'retry_verification', 'expected_version': view['state_version'],
               'expected_epoch': browser.epoch, 'idempotency_key': 'operator-authorized-once'}
    result = await client.post('/v1/handoffs/' + row['id'] + '/actions', json=request)
    assert result.status_code == 200, result.text
    assert result.json()['resolution'] == 'operator_retry_authorized'
    assert result.json()['execution_resumed'] is True and not result.json()['resume_pending']
    if lost_native:
        assert browser.id in runtime.closed and result.json()['session_id'] != browser.id
        assert len(runtime.created) == 2
    assert engine.store.get_challenge(browser.challenge_id)['state'] == 'detected'
    assert (await client.post('/v1/handoffs/' + row['id'] + '/actions', json=request)).status_code == 200
    assert (await drain(engine, run))['status'] == 'completed'
    assert len({session.session_id for session in engine.backend.sessions}) == 1
    final = engine.store.get_challenge(browser.challenge_id)
    assert final['state'] == 'resolved' and final['attempts'] == (0 if passive_recovery else 1)
    assert engine.store.get_document('challenge.episode', [old['id'], old['episode']])['snapshot'] == old
    assert engine.handoffs.list(run['job_id'], 'active') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('field,foreign', [('job_id', 'other-job'), ('profile_id', 'other-profile'),
    ('principal_id', 'other-principal'), ('agent_id', 'task:other-task')])
async def test_retry_rejects_foreign_browser_before_observing_or_closing(monkeypatch, field, foreign):
    from ore.operator_retry import retry_verification
    from ore.policy import AccessDenied
    job, task = {'id': 'current-job', 'mission': {}}, {'id': 'current-task'}
    row = {'job_id': job['id'], 'task_id': task['id'], 'challenge_id': 'episode', 'session_id': 'foreign',
           'checkpoint_url': 'https://journal.test/challenge', 'access_profile_ref': 'public'}
    session = SimpleNamespace(job_id=job['id'], profile_id='public', principal_id='operator', agent_id='task:' + task['id'])
    setattr(session, field, foreign)
    browser = SimpleNamespace(observe=AsyncMock(), close_session=AsyncMock())
    engine = SimpleNamespace(store=SimpleNamespace(get_challenge=lambda ident: {
        'id': ident, 'origin': 'https://journal.test', 'auth_context': 'public:operator'}),
        profile=lambda mission: {'id': 'public'}, browser=browser)
    broker = SimpleNamespace(remote=False, exists=lambda sid: True, session=lambda sid: session)
    monkeypatch.setattr('ore.operator_retry._current_execution', lambda *args: (job, task))
    monkeypatch.setattr('ore.operator_retry.retry_metadata', lambda *args: {'eligible': True})
    with pytest.raises(AccessDenied, match='does not belong'):
        await retry_verification(engine, broker, row, {'idempotency_key': 'foreign-rejected'})
    browser.observe.assert_not_awaited()
    browser.close_session.assert_not_awaited()
