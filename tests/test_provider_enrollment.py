"""Approved provider form effects stay scoped, single-use and secret-free."""
import asyncio
import json
from copy import deepcopy
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_engine import engine as engine_fixture

from ore.policy import AccessDenied
from ore.provider_enrollment import SELECTOR, ProviderEnrollment, setup_browser_observation
from ore.server import create_app
from ore.store import DocumentConflict
from ore.tools import ToolRuntime

engine = engine_fixture


class Fields:
    def __init__(self, page, target=None):
        self.page, self.target = page, target
    async def evaluate_all(self, script):
        return deepcopy(self.page.fields)
    def nth(self, target):
        return Fields(self.page, target)
    async def fill(self, value, **kwargs):
        self.page.values[self.target] = value
        self.page.effects.append(('fill', self.target))
    async def click(self, **kwargs):
        self.page.effects.append(('click', self.target))
        if self.page.fail_click:
            raise RuntimeError('DO_NOT_EXPOSE_SECRET')
    async def input_value(self, **kwargs):
        return self.page.values.get(self.target, '')


class Page:
    url = 'https://provider.test/register'
    def __init__(self):
        self.fields = [
            {'index': 0, 'tag': 'input', 'type': 'email', 'name': 'email', 'label': 'Email', 'form': 0},
            {'index': 1, 'tag': 'input', 'type': 'password', 'name': 'password', 'label': 'Password', 'form': 0},
            {'index': 2, 'tag': 'input', 'type': 'text', 'name': 'firstName', 'label': 'First name', 'form': 0},
            {'index': 3, 'tag': 'button', 'type': 'submit', 'name': '', 'label': 'Register', 'form': 0},
            {'index': 4, 'tag': 'input', 'type': 'text', 'name': 'apiKey', 'label': 'API key', 'form': 0},
        ]
        self.effects, self.values, self.fail_click = [], {}, False
    def locator(self, selector):
        assert selector == SELECTOR
        return Fields(self)
    async def title(self):
        return 'DO_NOT_EXPOSE_SECRET'


@pytest.fixture
async def setup(engine):
    engine.start = AsyncMock()
    app = create_app(engine)
    chat = engine.conversations.create()
    profile = {**engine.profile({}), 'id': 'enrollment-fixture'}
    engine.save_profile(profile)
    card = engine.connections.create({'provider': 'scopus', 'conversation_id': chat['id'], 'access_profile_ref': profile['id']})
    job = engine.create({'goal': 'Prepare provider account', 'allowed_origins': ['https://provider.test'],
        'operator_access': {'purpose': 'provider_setup'}, 'connection_id': card['id'], 'access_profile_ref': profile['id'],
        'allowed_capabilities': ['provider_form', 'browser_observe', 'browser_action']}, queued=False)
    row = engine.connections._read(card['id'])
    engine.secrets.set('fixture.username', 'person@protected.test')
    engine.secrets.set('fixture.password', 'DO_NOT_EXPOSE_SECRET')
    engine.connections._save(row, {'agent_job_id': job['id'], 'secret_refs': {
        'username': 'fixture.username', 'password': 'fixture.password'}})
    profile = engine.profile(job['mission'])
    session = SimpleNamespace(id='provider-form-session', job_id=job['id'], agent_id='agent:fixture',
        page=Page(), context=SimpleNamespace(desktop=False, companion=False), mission=job['mission'],
        policy=SimpleNamespace(profile=profile, check=AsyncMock()), control='agent', epoch=1,
        closed=False, closing=False, lock=asyncio.Lock(), profile_id=profile['id'])
    engine.browser.sessions = {session.id: session}
    engine.browser.get = lambda ident: engine.browser.sessions[ident]
    engine.browser.secrets = engine.secrets
    from ore.browser import BrowserManager
    engine.browser.store = engine.store
    engine.browser._authorize = MethodType(BrowserManager._authorize, engine.browser)
    control = engine.store.acquire_control(job['id'], session.id, session.agent_id, 'agent', None, 3600)
    session.epoch = control['epoch']
    enrollment = engine.provider_enrollment
    enrollment.save_details(card['id'], {'given_name': 'Test'})
    runtime = ToolRuntime(engine, job['id'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://ore.test',
                                headers={'Authorization': 'Bearer synthetic-test-operator'}) as client:
        yield engine, enrollment, runtime, session, card, client
    engine.browser.sessions.clear()


async def inspect(enrollment, runtime, session):
    return await enrollment.tool(runtime, {'operation': 'inspect', 'session_id': session.id, 'epoch': session.epoch})


def args(snapshot, op, **extra):
    return {'operation': op, **{k: snapshot[k] for k in ('session_id', 'epoch', 'url', 'form_fingerprint')}, **extra}


def approval(action, key='approve-fixture-001'):
    return {'expected_version': action['state_version'], 'idempotency_key': key}


async def test_known_user_fields_fill_without_extra_approval_and_values_never_return(setup):
    _engine, manager, runtime, session, card, _client = setup
    snap = await inspect(manager, runtime, session)
    assert snap['available_value_refs'] == ['given_name', 'password', 'username']
    result = await manager.tool(runtime, args(snap, 'fill', fields=[
        {'target': 0, 'value_ref': 'username'}, {'target': 1, 'value_ref': 'password'},
        {'target': 2, 'value_ref': 'given_name'}]))
    assert result['status'] == 'completed'
    assert session.page.values == {0: 'person@protected.test', 1: 'DO_NOT_EXPOSE_SECRET', 2: 'Test'}
    assert 'DO_NOT_EXPOSE_SECRET' not in json.dumps(result)
    assert 'person@protected.test' not in json.dumps(manager.list(card['id']))
    assert not any(effect[0] == 'click' for effect in session.page.effects)


async def test_final_click_waits_for_chat_approval_and_replay_cannot_click_twice(setup):
    _engine, manager, runtime, session, card, client = setup
    snap = await inspect(manager, runtime, session)
    pending = await manager.tool(runtime, args(snap, 'propose_click', target=3))
    assert pending['status'] == 'pending' and not session.page.effects
    response = await client.post(f"/v1/connections/{card['id']}/form-actions/{pending['id']}/approve", json=approval(pending))
    assert response.status_code == 200 and response.json()['status'] == 'completed'
    replay = await client.post(f"/v1/connections/{card['id']}/form-actions/{pending['id']}/approve", json=approval(pending))
    assert replay.status_code == 200 and session.page.effects == [('click', 3)]


@pytest.mark.parametrize('change', ['url', 'dom', 'profile', 'epoch'])
async def test_changed_reviewed_scope_never_executes(change, setup):
    _engine, manager, runtime, session, card, _client = setup
    pending = await manager.tool(runtime, args(await inspect(manager, runtime, session), 'propose_click', target=3))
    if change == 'url':
        session.page.url = 'https://provider.test/different'
    elif change == 'dom':
        session.page.fields[3]['label'] = 'Pay now'
    elif change == 'profile':
        session.policy.profile = {**session.policy.profile, 'changed': True}
    else:
        session.epoch += 1
    with pytest.raises((AccessDenied, DocumentConflict)):
        await manager.decide(card['id'], pending['id'], approval(pending), approve=True)
    assert not session.page.effects


async def test_uncertain_submit_is_not_replayed_even_with_original_approval(setup):
    engine, manager, runtime, session, card, _client = setup
    pending = await manager.tool(runtime, args(await inspect(manager, runtime, session), 'propose_click', target=3))
    session.page.fail_click = True
    with pytest.raises(DocumentConflict, match='uncertain'):
        await manager.decide(card['id'], pending['id'], approval(pending), approve=True)
    replay = await manager.decide(card['id'], pending['id'], approval(pending), approve=True)
    assert replay['status'] == 'uncertain' and session.page.effects == [('click', 3)]
    assert 'DO_NOT_EXPOSE_SECRET' not in json.dumps(manager.list(card['id']))
    recovered = ProviderEnrollment(engine)
    assert recovered.list(card['id'])['actions'][0]['status'] == 'uncertain'


@pytest.mark.parametrize('fields', [
    [{'target': 0, 'value_ref': 'password'}],
    [{'target': 2, 'value_ref': 'username'}],
    [{'target': 2, 'value_ref': 'invented'}],
    [{'target': 2, 'value_ref': 'given_name', 'value': 'Invented person'}],
])
async def test_agent_cannot_invent_values_or_type_password_into_public_field(fields, setup):
    _engine, manager, runtime, session, _card, _client = setup
    with pytest.raises((AccessDenied, ValueError)):
        await manager.tool(runtime, args(await inspect(manager, runtime, session), 'fill', fields=fields))
    assert not session.page.effects


async def test_capture_key_requires_exact_target_review_and_stores_only_encrypted_value(setup):
    engine, manager, runtime, session, card, _client = setup
    session.page.values[4] = 'UNSEEN_PROVIDER_ISSUED_KEY'
    pending = await manager.tool(runtime, args(await inspect(manager, runtime, session), 'capture_key', target=4))
    assert 'UNSEEN_PROVIDER_ISSUED_KEY' not in json.dumps(pending)
    result = await manager.decide(card['id'], pending['id'], approval(pending), approve=True)
    assert result['status'] == 'completed'
    ref = engine.connections._read(card['id'])['secret_refs']['api_key']
    assert engine.secrets.get(ref) == 'UNSEEN_PROVIDER_ISSUED_KEY'
    assert 'UNSEEN_PROVIDER_ISSUED_KEY' not in json.dumps(manager.list(card['id']))


async def test_provider_browser_observation_never_reads_body_or_screenshot(setup):
    engine, _manager, _runtime, session, _card, _client = setup
    engine.browser.summary = AsyncMock(return_value={'session_id': session.id, 'epoch': 1})
    session.page.fields[4]['label'] = 'API key: UNSEEN_PROVIDER_ISSUED_KEY'
    result = await setup_browser_observation(engine.browser, session)
    serialized = json.dumps(result)
    assert result['text'] == '' and result['image_url'] is None
    assert 'UNSEEN_PROVIDER_ISSUED_KEY' not in serialized and 'DO_NOT_EXPOSE_SECRET' not in serialized
    assert result['elements'][4]['field_hint'] == 'api_key'


async def test_generic_collection_cannot_use_provider_form_tools(setup):
    engine, manager, _runtime, session, _card, _client = setup
    other = engine.create({'goal': 'Collect journal articles'}, queued=False)
    with pytest.raises(AccessDenied):
        await manager.tool(ToolRuntime(engine, other['id']), {'operation': 'inspect', 'session_id': session.id, 'epoch': 1})


async def test_details_endpoint_rejects_unknown_fields_and_unauthenticated_mutations(setup):
    _engine, _manager, _runtime, _session, card, client = setup
    endpoint = f"/v1/connections/{card['id']}/enrollment/details"
    bad = await client.post(endpoint, json={'values': {'password': 'DO_NOT_EXPOSE_SECRET'}})
    assert bad.status_code == 422 and 'DO_NOT_EXPOSE_SECRET' not in bad.text
    accepted = await client.post(endpoint, json={'values': {'family_name': 'Person'}})
    assert accepted.status_code == 200 and accepted.json()['values'] == {'given_name': 'Test', 'family_name': 'Person'}
    denied = await client.post(endpoint, json={'values': {'given_name': 'Other'}}, headers={'Authorization': 'Bearer invalid'})
    assert denied.status_code == 401


@pytest.mark.parametrize('input_type', ['text', 'password', 'number'])
async def test_mfa_code_is_protected_expiring_single_use_input(input_type, setup):
    engine, manager, runtime, session, card, client = setup
    session.page.fields.append({'index': 5, 'tag': 'input', 'type': input_type, 'name': 'otp', 'label': 'Verification code', 'form': 0})
    response = await client.post(f"/v1/connections/{card['id']}/enrollment/secret", json={'field': 'mfa_code', 'value': '123789'})
    assert response.status_code == 200 and '123789' not in response.text
    snap = await inspect(manager, runtime, session)
    assert 'mfa_code' in snap['available_value_refs']
    result = await manager.tool(runtime, args(snap, 'fill', fields=[{'target': 5, 'value_ref': 'mfa_code'}]))
    assert result['status'] == 'completed' and session.page.values[5] == '123789'
    assert 'mfa_code' not in manager.details(card['id'])['configured_protected_fields']
    ref = engine.store.get_document('connection.enrollment_code', card['id'])['ref']
    assert engine.secrets.get(ref) == ''
    with pytest.raises(AccessDenied, match='fresh'):
        manager._value(engine.connections._read(card['id']), 'mfa_code')


async def test_raw_form_values_are_rejected_before_any_tool_event(setup):
    engine, manager, runtime, session, _card, _client = setup
    snap = await inspect(manager, runtime, session)
    before = len(engine.store.events(runtime.job_id))
    with pytest.raises(ValueError):
        await runtime.execute('provider_form', args(snap, 'fill', fields=[{'target': 1, 'value_ref': 'password', 'value': 'DO_NOT_EXPOSE_SECRET'}]))
    assert len(engine.store.events(runtime.job_id)) == before


async def test_credentials_added_after_setup_preserve_browser_authority(setup):
    engine, manager, runtime, session, card, client = setup
    stored = (await client.get(f"/v1/connections/{card['id']}")).json()
    response = await client.post(f"/v1/connections/{card['id']}/secret", json={
        'field': 'username', 'value': 'added@protected.test', 'expected_version': stored['state_version'],
        'idempotency_key': 'store-after-bootstrap'})
    assert response.status_code == 200
    snap = await inspect(manager, runtime, session)
    result = await manager.tool(runtime, args(snap, 'fill', fields=[{'target': 0, 'value_ref': 'username'}]))
    assert result['status'] == 'completed' and session.page.values[0] == 'added@protected.test'
    changed = engine.profile(runtime.job()['mission'])
    changed['allow_private_network'] = True
    engine.save_profile(changed)
    with pytest.raises(DocumentConflict, match='profile'):
        await inspect(manager, runtime, session)


async def test_native_proposal_pauses_and_approved_action_resumes_same_agent_browser(setup, monkeypatch):
    from test_workflow import one
    from test_workflow_native import ScriptedSession
    engine, manager, runtime, session, card, client = setup
    monkeypatch.setattr('ore.workflow_native.AgentSession', ScriptedSession)
    backend = SimpleNamespace(sessions=[], prompts=[], rounds={}, results=[], usage_totals={}, timeouts=[], close=AsyncMock())
    engine.backend = backend
    engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda *unused, **unused_keywords: {'model': 'fixture', 'effort': 'high', 'mode': 'fixed'}
    mission = {**runtime.job()['mission'], 'agent_runtime': 'native', 'model': 'fixture', 'model_policy': 'fixed'}
    run = engine.workflows.create_run({'goal': 'Prepare the requested provider account', 'mission': mission,
        'workflow': {'schema_version': 'ore.workflow/v2', 'nodes': [{'id': 'provider-setup', 'kind': 'agent',
            'allowed_tools': ['provider_form', 'browser_observe'],
            'checks': [{'op': 'eq', 'left': {'$ref': 'output.done'}, 'right': True}]}]}})
    row = engine.connections._read(card['id'])
    engine.connections._save(row, {'workflow_run_id': run['id'], 'agent_job_id': run['job_id']})
    job = engine.store.get_job(run['job_id'])
    session.job_id, session.mission = job['id'], job['mission']
    session.policy.profile = engine.profile(job['mission'])
    async def observed(sid, **unused):
        return await setup_browser_observation(engine.browser, session)
    async def summary(unused):
        return {'session_id': session.id, 'epoch': session.epoch, 'url': session.page.url}
    engine.browser.observe, engine.browser.summary = observed, summary
    async def script(native, prompt):
        round_number = backend.rounds['provider-setup']
        if round_number == 1:
            session.agent_id = 'task:' + native.lease['task_id']
            control = engine.store.acquire_control(job['id'], session.id, session.agent_id, 'agent', None, 3600)
            session.epoch = control['epoch']
            snap = await native.call('provider_form', {'operation': 'inspect', 'session_id': session.id, 'epoch': session.epoch})
            await native.call('provider_form', args(snap, 'propose_click', target=3))
            assert native.yield_reason
        else:
            old_epoch = session.epoch
            observed = await native.call('browser_observe', {'session_id': session.id})
            assert observed['epoch'] > old_epoch
            assert session.agent_id == 'task:' + native.lease['task_id']
            await native.call('workflow.finish', {'output': {'done': True}})
    backend.script = script
    await engine.workflows.start_run(run['id'])
    await one(engine, run)
    assert engine.workflows.get_run(run['id'])['status'] == 'awaiting_user', [node.get('error') for node in engine.workflows.nodes(run['id'])]
    pending = manager.list(card['id'])['actions'][-1]
    handoff = engine.handoffs.get(pending['handoff_id'])
    assert handoff['session_id'] == session.id and handoff['job_id'] == run['job_id']
    response = await client.post(f"/v1/connections/{card['id']}/form-actions/{pending['id']}/approve", json=approval(pending))
    assert response.status_code == 200, response.text
    assert engine.handoffs.get(handoff['id'])['status'] == 'resolved'
    await one(engine, run)
    assert engine.workflows.get_run(run['id'])['status'] == 'completed'
    assert len({native.thread_id for native in backend.sessions}) == 1
    assert session.page.effects == [('click', 3)]


async def test_setup_credential_download_is_cancelled_without_metadata_or_file_access(setup):
    from ore.provider_enrollment import block_setup_download
    engine, _manager, _runtime, session, _card, _client = setup
    item = SimpleNamespace(cancel=AsyncMock(), save_as=AsyncMock(), suggested_filename='DO_NOT_EXPOSE_SECRET.txt', url='https://provider.test/DO_NOT_EXPOSE_SECRET')
    engine.browser._emit = AsyncMock()
    assert await block_setup_download(engine.browser, session, item)
    item.cancel.assert_awaited_once()
    item.save_as.assert_not_awaited()
    assert 'DO_NOT_EXPOSE_SECRET' not in repr(engine.browser._emit.call_args)
    session.mission = {'goal': 'Normal corpus download'}
    assert not await block_setup_download(engine.browser, session, item)


async def test_decline_remains_possible_when_browser_epoch_changed(setup):
    _engine, manager, runtime, session, card, _client = setup
    action = await manager.tool(runtime, args(await inspect(manager, runtime, session), 'propose_click', target=3))
    session.epoch += 1
    result = await manager.decide(card['id'], action['id'], approval(action), approve=False)
    assert result['status'] == 'rejected' and not session.page.effects


async def test_form_approval_never_resolves_other_challenge_handoff(setup):
    engine, manager, runtime, session, card, _client = setup
    other = engine.handoffs.create(runtime.job_id, 'challenge', 'Prior challenge still needs verification',
        session_id=session.id, context={'url': session.page.url, 'epoch': session.epoch})
    action = await manager.tool(runtime, args(await inspect(manager, runtime, session), 'propose_click', target=3))
    assert action['handoff_id'] != other['id']
    await manager.decide(card['id'], action['id'], approval(action), approve=True)
    assert engine.handoffs.get(other['id'])['status'] == 'needs_user'
