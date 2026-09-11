"""Resumed native nodes retain only their own verified browser authority."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore.browser import BrowserManager, BrowserSession
from ore.browser_resume import rebind_workflow_browser
from ore.handoffs import HandoffService
from ore.policy import AccessDenied, AccessPolicy, RateLimiter
from ore.tools import ToolRuntime
from test_browser import site, browser_dependencies
from test_engine import engine
from test_server import client, login
from test_workflow_native import ScriptedSession, plan, agent, drain


@pytest.mark.browser
async def test_native_user_handoff_resume_keeps_browser_for_new_task(client, engine, site, browser_dependencies, monkeypatch):
    await login(client)
    monkeypatch.setattr('ore.workflow_native.AgentSession', ScriptedSession)
    engine.backend = SimpleNamespace(sessions=[], prompts=[], rounds={}, results=[], usage_totals={}, timeouts=[], close=AsyncMock())
    engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'fixture', 'effort': 'high', 'mode': 'fixed'}
    engine.start = AsyncMock()
    engine.save_profile({'id': 'public', 'allow_private_network': True, 'sources': {}})
    engine.browser = BrowserManager(engine.settings, RateLimiter(), store=engine.store, secrets=engine.secrets, on_event=engine.event)
    seen = {}
    async def script(session, prompt):
        if engine.backend.rounds['parent'] == 1:
            opened = await session.call('browser_open', {'url': site + '/challenge'})
            assert opened['challenge_detected']
            seen['id'] = opened['session_id']
            await session.call('handoff', {'session_id': seen['id'], 'reason': 'Complete controlled access fixture'})
        else:
            state = await session.call('state', {})
            assert [row['id'] for row in state['browsers']] == [seen['id']]
            assert state['browsers'][0]['epoch'] > seen['returned_epoch']
            observed = await session.call('browser_observe', {'session_id': seen['id']})
            assert observed['control'] == 'agent' and not observed['challenge_detected']
            assert 'independently observed target content' in observed['text']
            await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent(completion={'required_tools': [{'tool': 'browser_observe',
        'min_count': 1, 'checks': [{'op': 'eq', 'left': {'$ref': 'output.challenge_detected'}, 'right': False}]}]})],
        mission={'allowed_origins': [site], 'artifact_roles': [], 'completeness': 'bounded',
                 'limits': {'origin_min_interval_seconds': 0},
                 'on_challenge': {'policy_version': 2, 'max_attempts_per_episode': 3, 'max_elapsed_seconds': 120}}))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'awaiting_user'
    browser = engine.browser.get(seen['id'])
    old_actor, challenge_id = browser.agent_id, browser.challenge_id
    before = engine.store.get_challenge(challenge_id)
    await engine.browser.action(browser.id, 'click', {'selector': '#solve', 'epoch': browser.epoch, 'screenshot': False}, owner='human')
    row = engine.handoffs.list(run['job_id'], 'active')[0]
    response = await client.post('/v1/handoffs/' + row['id'] + '/actions', json={
        'action': 'resume', 'expected_version': row['state_version'], 'expected_epoch': browser.epoch,
        'idempotency_key': 'resume-native-browser'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'resolved'
    seen['returned_epoch'] = browser.epoch
    assert browser.agent_id == old_actor
    assert (await drain(engine, run))['status'] == 'completed'
    assert browser.agent_id != old_actor and len(engine.browser.sessions) == 1
    after = engine.store.get_challenge(challenge_id)
    assert after['state'] == 'resolved' and after['attempts'] == before['attempts']
    assert after['deadline_at'] == before['deadline_at']
    assert len({session.session_id for session in engine.backend.sessions}) == 1
    assert len(engine.store.tasks(run['job_id'])) == 2


@pytest.fixture
async def resumed_browser(engine):
    engine.start = AsyncMock()
    run = engine.workflows.create_run(plan([agent()], mission={'artifact_roles': [], 'allowed_origins': ['https://example.org']}))
    await engine.workflows.start_run(run['id'])
    old = engine.store.claim_task('old-worker', job_id=run['job_id'])
    job = engine.store.get_job(run['job_id'])
    profile = engine.profile(job['mission'])
    session = BrowserSession('resumed-browser', job['id'], SimpleNamespace(), SimpleNamespace(url='https://example.org/'),
        AccessPolicy(deepcopy(job['mission']), deepcopy(profile)), profile['id'], 0,
        mission=deepcopy(job['mission']), agent_id='task:' + old['id'])
    session.epoch = engine.store.acquire_control(job['id'], session.id, session.agent_id)['epoch']
    engine.workflows._fail(old, 'awaiting_user', {'code': 'fixture_handoff'})
    await engine.workflows.resume(run['id'])
    new = engine.store.claim_task('new-worker', job_id=run['job_id'])
    with engine.store._tx() as conn:
        current_run, node, raw_job = engine.workflows._owned(conn, new)
        node['status'] = 'running'
        engine.workflows._save(conn, 'workflow.node', [run['id'], node['id']], node, raw_job)
    engine.browser = SimpleNamespace(_publish_control=AsyncMock(), close=AsyncMock())
    return engine, ToolRuntime(engine, job['id'], new), session, old


@pytest.mark.parametrize('change', ['job', 'node', 'mission', 'profile', 'principal', 'human', 'form', 'reservation', 'active_old', 'lease'])
async def test_rebind_rejects_changed_authority_or_unsettled_owner(resumed_browser, change):
    engine, runtime, session, old = resumed_browser
    before = session.agent_id, session.epoch
    if change == 'job': session.job_id = 'another-job'
    elif change == 'node':
        from sqlalchemy import update
        from ore.store import tasks
        with engine.store._tx() as conn:
            conn.execute(update(tasks).where(tasks.c.id == old['id']).values(input={**old['input'], 'node_id': 'sibling'}))
    elif change == 'mission': session.mission['allowed_origins'].append('https://elsewhere.example')
    elif change == 'profile': session.policy.profile = {**session.policy.profile, 'origins': ['https://elsewhere.example']}
    elif change == 'principal': session.principal_id = 'another-operator'
    elif change == 'human': session.control = 'human'
    elif change == 'form': session.enrollment_pending = 'unapproved-form'
    elif change == 'reservation': session.challenge_reservation = {'id': 'attempt-still-inflight'}
    elif change == 'active_old':
        from sqlalchemy import update
        from ore.store import tasks
        with engine.store._tx() as conn:
            conn.execute(update(tasks).where(tasks.c.id == old['id']).values(state='running'))
    else: runtime.task = {**runtime.task, 'fence': runtime.task['fence'] + 1}
    from ore.store import LeaseLost
    with pytest.raises((AccessDenied, LeaseLost)):
        await rebind_workflow_browser(runtime, session)
    assert (session.agent_id, session.epoch) == before
    assert engine.browser._publish_control.await_count == 0


async def test_rebind_preserves_session_and_advances_control_epoch(resumed_browser):
    engine, runtime, session, old = resumed_browser
    epoch = session.epoch
    assert await rebind_workflow_browser(runtime, session)
    assert session.agent_id == runtime.actor_id and session.epoch > epoch
    engine.store.validate_control(runtime.job_id, session.id, runtime.actor_id, session.epoch)
    assert len([event for event in engine.store.events(runtime.job_id) if event['type'] == 'browser.task_rebound']) == 1
    assert await rebind_workflow_browser(runtime, session)
    assert engine.browser._publish_control.await_count == 1


async def test_rebind_retains_only_committed_native_desktop_scope(resumed_browser):
    from ore.desktop_api import _session_policy
    engine, runtime, session, old = resumed_browser
    job = runtime.job()
    copied, profile, _ = _session_policy(job['mission'], engine.profile(job['mission']))
    session.context.desktop = True
    session.mission = copied
    session.policy = AccessPolicy(copied, profile)
    row = engine.handoffs.create(job['id'], 'browser', 'Desktop access', session_id=session.id,
        task_id=old['id'], context={'url': 'https://example.org/article'})
    engine.handoffs.update(row['id'], {'status': 'resolved', 'browser_transport': 'desktop_chrome',
        'desktop_attachment': {'scope': 'operator_session_only'}})
    before_mission, before_profile = deepcopy(session.mission), deepcopy(session.policy.profile)
    assert await rebind_workflow_browser(runtime, session)
    assert session.mission == before_mission and session.policy.profile == before_profile
    assert session.agent_id == runtime.actor_id


async def test_rebind_native_tool_checkpoint_keeps_original_mission_authority(resumed_browser, monkeypatch):
    from ore.models import canonical_digest
    from ore.native_scope import native_scope_copy
    engine, runtime, session, old = resumed_browser
    job = runtime.job()
    profile = engine.profile(job['mission'])
    copied, native_profile = native_scope_copy(job['mission'], profile, 'https://example.org/article')
    session.context.desktop = True
    session.mission, session.policy = copied, AccessPolicy(copied, native_profile)
    session.native_scope_binding = {'checkpoint': 'https://example.org/article',
        'mission_digest': canonical_digest(job['mission']), 'profile_digest': canonical_digest(profile)}
    # This test exercises authority reconstruction; DNS is independently tested
    # by native scope tests and does not need a real external connection here.
    monkeypatch.setattr(AccessPolicy, 'check', AsyncMock())
    assert await rebind_workflow_browser(runtime, session)
    assert session.agent_id == runtime.actor_id and session.mission == copied
    assert runtime.job()['mission'] == job['mission']
