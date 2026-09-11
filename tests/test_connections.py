"""Persistent connection cards and protected bootstrap with entirely fake auth."""
from copy import deepcopy
import json
from unittest.mock import AsyncMock
import httpx
import pytest
from ore.connections import ConnectionManager
from ore.handoffs import BrowserAccess
from ore.operator_access import attachment_mission, operator_mission
from ore.policy import AccessDenied
from ore.server import create_app
from ore.source_policy import authorize_operation
from test_engine import engine


class FakeAuth:
    def __init__(self):
        self.status = 'not_authenticated'; self.starts = 0; self.codes = []; self.callback = None
    async def auth_status(self): return {'status': self.status, 'mode': 'official_cli'}
    async def quota_status(self): return {'status': 'unknown', 'windows': [], 'stale': False, 'ordinary_usage_allowed': None}
    async def start_login(self, on_event=None):
        self.starts += 1; self.callback = on_event
        return {'status': 'awaiting_auth', 'login_id': 'fixture-login',
                'verification_url': 'https://auth.openai.com/codex/device', 'user_code': 'FIXTURE-CODE'}
    async def cancel_login(self, ident): return {'status': 'cancelled', 'acknowledged': True}
    async def submit_login(self, ident, code): self.codes.append((ident, code)); return {'status': 'awaiting_auth'}
    async def close(self): pass


@pytest.fixture
async def api(engine):
    engine.start = AsyncMock()
    engine.provider_auth = {'codex': FakeAuth(), 'claude_code': FakeAuth()}
    app = create_app(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://ore.test',
                                headers={'Authorization': 'Bearer synthetic-test-operator'}) as client:
        yield client, engine


def action(card, name, key='fixture-action-1', **extra):
    return {'action': name, 'expected_version': card['state_version'], 'idempotency_key': key, **extra}


async def create(api, provider='codex', **values):
    client, _ = api
    response = await client.post('/v1/connections', json={'provider': provider, **values})
    assert response.status_code == 201, response.text
    return response.json()


async def test_login_bootstrap_durable_without_models_and_codes_never_persist(api):
    client, engine = api; card = await create(api)
    response = await client.post('/v1/connections/'+card['id']+'/actions', json=action(card, 'start_login'))
    assert response.status_code == 200, response.text
    pending = response.json(); assert pending['user_code'] == 'FIXTURE-CODE'
    assert pending['status'] == 'awaiting_auth'
    assert engine.backend.calls == [] and engine.store.list_jobs() == []
    persisted = engine.store.get_document('connection', card['id'])
    assert 'FIXTURE-CODE' not in json.dumps(persisted)
    again = await client.post('/v1/connections/'+card['id']+'/actions', json=action(card, 'start_login'))
    assert again.status_code == 200 and engine.provider_auth['codex'].starts == 1
    await engine.provider_auth['codex'].callback({'login_id': 'fixture-login', 'status': 'ready'})
    ready = (await client.get('/v1/connections/'+card['id'])).json()
    assert ready['status'] == 'ready' and 'user_code' not in ready


async def test_global_model_card_remains_visible_after_conversation_created(api):
    client, engine = api; card = await create(api)
    conversation = engine.conversations.create({})
    rows = (await client.get('/v1/connections', params={'conversation_id': conversation['id']})).json()
    assert card['id'] in [row['id'] for row in rows]
    assert (await client.get('/v1/connections', params={'conversation_id': 'missing'})).status_code == 404


async def test_card_creation_is_idempotent_and_stale_action_rejected(api):
    client, engine = api; card = await create(api)
    assert (await create(api))['id'] == card['id']
    bad = action(card, 'start_login'); bad['expected_version'] += 5
    assert (await client.post('/v1/connections/'+card['id']+'/actions', json=bad)).status_code == 409
    assert engine.provider_auth['codex'].starts == 0


async def test_pending_source_is_excluded_and_setup_is_conversation_linked(api):
    client, engine = api; conversation = engine.conversations.create({})
    card = await create(api, 'wos', conversation_id=conversation['id'])
    opened = (await client.post('/v1/connections/'+card['id']+'/actions', json=action(card, 'open_provider'))).json()
    assert opened['handoff_id'] and opened['conversation_id'] == conversation['id']
    job = engine.store.get_job(opened['job_id'])
    assert job['mission']['allowed_origins'] == ['https://developer.clarivate.com']
    assert job['mission']['urls'] == ['https://developer.clarivate.com/apis/wos-starter']
    pending = await client.post('/v1/connections/'+card['id']+'/actions', json=action(opened, 'mark_pending', 'fixture-pending'))
    assert pending.status_code == 200 and pending.json()['status'] == 'approval_pending'
    profile = engine.profile(job['mission'])
    assert authorize_operation({'sources':['wos']}, profile, 'wos', 'search')['code'] == 'approval_pending'
    assert engine.backend.calls == []


async def test_direct_source_setup_resolves_registry_docs_and_links_card(api):
    client, engine = api; conversation = engine.conversations.create({})
    response = await client.post('/v1/sources/crossref/setup', json={'conversation_id': conversation['id']})
    assert response.status_code == 200, response.text
    handoff = response.json()
    assert handoff['checkpoint_url'].startswith('https://www.crossref.org/')
    rows = (await client.get('/v1/connections', params={'conversation_id': conversation['id']})).json()
    assert len(rows) == 1 and rows[0]['handoff_id'] == handoff['id']
    assert engine.store.get_job(handoff['job_id'])['mission']['allowed_origins'] == ['https://www.crossref.org']


async def test_protected_secret_never_enters_cards_or_events_and_does_not_mark_ready(api):
    client, engine = api; card = await create(api, 'scopus')
    body = {'field': 'api_key', 'value': 'synthetic-secret-never-echo', 'expected_version': card['state_version'], 'idempotency_key': 'secret-fixture'}
    response = await client.post('/v1/connections/'+card['id']+'/secret', json=body)
    assert response.status_code == 200, response.text
    result = response.json(); assert result['configured_fields'] == ['api_key'] and result['status'] != 'ready'
    assert body['value'] not in response.text
    stored = engine.store.get_document('connection', card['id'])
    assert body['value'] not in json.dumps(stored)
    assert engine.secrets.get(stored['secret_refs']['api_key']) == body['value']
    invalid = {**body, 'expected_version': body['value']}
    denied = await client.post('/v1/connections/'+card['id']+'/secret', json=invalid)
    assert denied.status_code == 422 and body['value'] not in denied.text
    model_card = await create(api)
    assert (await client.post('/v1/connections/'+model_card['id']+'/secret', json={**body, 'expected_version': model_card['state_version']})).status_code == 403


async def test_claude_completion_code_is_transient_and_duplicate_does_not_submit_twice(api):
    client, engine = api; card = await create(api, 'claude_code')
    pending = (await client.post('/v1/connections/'+card['id']+'/actions', json=action(card, 'start_login'))).json()
    assert pending['login_code_required'] is True
    payload = {'code': 'synthetic-official-code', 'expected_version': pending['state_version'], 'idempotency_key': 'login-code-fixture'}
    for _ in range(2):
        response = await client.post('/v1/connections/'+card['id']+'/login-code', json=payload)
        assert response.status_code == 200, response.text
        assert payload['code'] not in response.text
    assert len(engine.provider_auth['claude_code'].codes) == 1
    assert payload['code'] not in json.dumps(engine.store.get_document('connection', card['id']))


async def test_source_agent_plan_scopes_tools_and_waits_for_model_auth(api):
    client, engine = api; card = await create(api, 'wos')
    waiting = (await client.post('/v1/connections/'+card['id']+'/actions', json=action(card, 'request_agent'))).json()
    assert waiting['status'] == 'awaiting_model_auth' and engine.backend.calls == []
    engine.provider_auth['codex'].status = 'ready'
    engine.workflows.start_run = AsyncMock()
    response = await client.post('/v1/connections/'+card['id']+'/actions', json=action(waiting, 'request_agent', 'agent-ready-fixture'))
    assert response.status_code == 200, response.text
    row = engine.store.get_document('connection', card['id'])
    run = engine.workflows.get_run(row['workflow_run_id'])
    job = engine.store.get_job(run['job_id'])
    assert job['mission']['allowed_origins'] == ['https://developer.clarivate.com']
    assert job['mission']['budget']['max_turns'] == 12
    assert set(job['mission']['allowed_capabilities']) == {'state','browser_open','browser_observe','browser_action','handoff'}
    assert 'terms' in engine.workflows.nodes(run['id'])[0]['spec']['goal']
    engine.workflows.start_run.assert_awaited_once()
    assert engine.backend.calls == []


async def test_provider_status_is_distinct_from_request_budget_and_secrets(api):
    client, engine = api; engine.provider_auth['codex'].status = 'ready'
    response = await client.get('/v1/providers/status')
    assert response.status_code == 200
    assert response.json()['scope'] == 'provider_account_not_request_budget'
    assert 'source_quotas' in response.json() and engine.backend.calls == []


def test_legacy_manual_attachment_is_exact_checkpoint_copy_not_mutation():
    job = {'id': 'job', 'mission': {'sources': [], 'artifact_roles': [], 'allowed_origins': []}}
    handoff = {'id': 'request', 'kind': 'challenge', 'checkpoint_url': 'https://www.scopus.com/start?private=value'}
    before = deepcopy(job)
    mission, basis = attachment_mission(job, handoff)
    assert basis == 'persisted_manual_checkpoint' and mission['allowed_origins'] == ['https://www.scopus.com']
    assert job == before


@pytest.mark.parametrize('change', [{'workflow_run_id':'run'}, {'rune':{'id':'retrieval'}}, {'mission':{'artifact_roles':['main_pdf']}}, {'mission':{'sources':['pubmed']}}])
def test_legacy_fallback_never_grants_collection_scope(change):
    job = {'id':'job','mission':{'sources':[],'artifact_roles':[]},**change}
    with pytest.raises(AccessDenied): attachment_mission(job, {'id':'h','kind':'browser','checkpoint_url':'https://example.org/'})


@pytest.mark.parametrize('url', [None, '', 'file:///etc/passwd', 'https://user:password@example.org/', 'https://*.example.org/', 'https://example.org/\n'])
def test_operator_url_rejects_unsafe_or_unbounded_origin(url):
    with pytest.raises(AccessDenied): operator_mission(url)


@pytest.mark.parametrize('provider', ['openai', 'anthropic'])
async def test_api_provider_has_protected_reference_without_claimed_entitlement(api, provider):
    client, engine = api; card = await create(api, provider)
    assert card['kind'] == 'model' and card['credential_fields'] == ['api_key']
    secret = 'synthetic-'+provider+'-private-key'
    response = await client.post('/v1/connections/'+card['id']+'/secret', json={
        'field':'api_key', 'value':secret, 'expected_version':card['state_version'], 'idempotency_key':'api-fixture-key'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert secret not in response.text and result['status'] == 'configured_unverified'
    assert result['backend']['kind'] == provider and engine.secrets.get(result['backend']['api_key_ref']) == secret
    assert 'start_login' not in result['actions'] and engine.backend.calls == []
    conversation = engine.conversations.create({})
    scoped = (await client.get('/v1/connections', params={'conversation_id':conversation['id']})).json()
    assert card['id'] in [r['id'] for r in scoped]


async def test_credential_storage_failure_never_echoes_input_and_is_reconcilable(api, monkeypatch):
    client, engine = api; card = await create(api, 'scopus'); secret='synthetic-secret-write-failure'
    def fail(ref, value): raise ValueError('private provider error '+value)
    monkeypatch.setattr(engine.secrets, 'set', fail)
    response = await client.post('/v1/connections/'+card['id']+'/secret', json={
        'field':'api_key', 'value':secret, 'expected_version':card['state_version'], 'idempotency_key':'failed-secret-write'})
    assert response.status_code == 422 and secret not in response.text
    saved = engine.store.get_document('connection',card['id'])
    assert saved['status'] == 'needs_review' and saved['inflight'] is None
    assert secret not in json.dumps(saved)


async def test_provider_status_strips_adapter_identity_and_private_extensions(api):
    client, engine = api; adapter=engine.provider_auth['codex']
    adapter.auth_status=AsyncMock(return_value={'status':'ready','email':'private-fixture','account_id':'private-fixture'})
    adapter.quota_status=AsyncMock(return_value={'status':'available','secret':'private-fixture',
        'windows':[{'id':'hour','used_percent':20,'access_token':'private-fixture'}]})
    adapter.usage_status=AsyncMock(return_value={'status':'available','credential':'private-fixture',
        'summary':{'lifetimeTokens':123,'secret':'private-fixture'}})
    response=await client.get('/v1/providers/status?include_usage=true')
    assert response.status_code == 200 and 'private-fixture' not in response.text
    item=next(x for x in response.json()['providers'] if x['provider']=='codex')
    assert item['usage']['summary']=={'lifetimeTokens':123}


async def test_setup_agent_current_handoff_and_unconfirmed_cancel_remain_visible(api):
    client, engine = api; card=await create(api,'wos');engine.provider_auth['codex'].status='ready'
    engine.workflows.start_run=AsyncMock()
    started=(await client.post('/v1/connections/'+card['id']+'/actions',json=action(card,'request_agent'))).json()
    stored=engine.store.get_document('connection',card['id']);agent_job_id=stored['agent_job_id']
    handoff=engine.handoffs.create(agent_job_id,'browser','Authentication requires user input',
        context={'url':'https://developer.clarivate.com/apis/wos-starter'})
    current=(await client.get('/v1/connections/'+card['id'])).json()
    assert current['handoff_id']==handoff['id']
    engine.workflows.interrupt=AsyncMock(return_value={'status':'interrupting','interruptions':[{'status':'unconfirmed'}]})
    stopped=await client.post('/v1/connections/'+card['id']+'/actions',json=action(current,'cancel','stop-agent-fixture'))
    assert stopped.status_code==200 and stopped.json()['status']=='cancel_unconfirmed'
    assert engine.handoffs.get(handoff['id'])['status']!='cancelled'


@pytest.mark.parametrize('name,args', [
    ('browser_action', {'session_id':'fixture','epoch':1,'action':'click','x':10,'y':10}),
    ('browser_action', {'session_id':'fixture','epoch':1,'action':'type','text':'fixture'}),
    ('browser_action', {'session_id':'fixture','epoch':1,'action':'key','key':'Enter'}),
    ('fetch', {'url':'https://provider.test/submit'}),
])
async def test_setup_mutations_denied_before_dispatch_and_public_event(engine, name, args):
    from ore.tools import ToolRuntime
    job=engine.create({**operator_mission('https://provider.test/'),
        'operator_access':{'purpose':'provider_setup','form_mutations':'operator_only'}},queued=False)
    runtime=ToolRuntime(engine,job['id']);runtime._execute=AsyncMock()
    before=engine.store.events(job['id'])
    with pytest.raises(AccessDenied): await runtime.execute(name,args)
    runtime._execute.assert_not_awaited()
    assert engine.store.events(job['id'])==before


@pytest.mark.parametrize('action_name', ['navigate','scroll','wait','back','tab'])
def test_setup_read_navigation_envelope(action_name):
    from ore.connection_agent import assert_setup_action
    assert_setup_action({'operator_access':{'purpose':'provider_setup'}},'browser_action',{'action':action_name})
    assert_setup_action({'operator_access':{'purpose':'provider_setup'}},'browser_observe',{})


async def test_adhoc_browser_new_job_saves_explicit_origin_and_conversation(api, monkeypatch):
    client,engine=api;conversation=engine.conversations.create({});browser=engine.connections.browser
    browser.create=AsyncMock(return_value='fixture-browser')
    browser.command=AsyncMock(return_value={'epoch':1})
    browser.summary=AsyncMock(return_value={'id':'fixture-browser'})
    response=await client.post('/v1/browser/sessions',json={'url':'https://provider.test/start', 'conversation_id':conversation['id']})
    assert response.status_code==200,response.text
    h=engine.handoffs.get(response.json()['handoff_id']);job=engine.store.get_job(h['job_id'])
    assert job['mission']['allowed_origins']==['https://provider.test']
    assert job['mission']['conversation_id']==conversation['id']
    assert job['mission']['sources']==[] and job['mission']['artifact_roles']==[]
    jobs=len(engine.store.list_jobs())
    missing=await client.post('/v1/browser/sessions',json={})
    assert missing.status_code==403 and len(engine.store.list_jobs())==jobs


async def test_source_card_identity_survives_automatic_public_profile_clone(api):
    client,engine=api;card=await create(api,'wos')
    await client.post('/v1/connections/'+card['id']+'/actions',json=action(card,'open_provider'))
    assert (await create(api,'wos'))['id']==card['id']


async def test_pending_cli_url_and_completion_update_on_poll_without_model(api):
    client,engine=api;card=await create(api,'claude_code')
    pending=(await client.post('/v1/connections/'+card['id']+'/actions',json=action(card,'start_login'))).json()
    adapter=engine.provider_auth['claude_code']
    adapter.login_status=AsyncMock(return_value={'status':'awaiting_auth','login_id':'fixture-login',
        'verification_url':'https://claude.ai/oauth/authorize'})
    polled=(await client.get('/v1/connections/'+card['id'])).json()
    assert polled['verification_url']=='https://claude.ai/oauth/authorize'
    adapter.login_status.return_value={'status':'ready','login_id':'fixture-login'}
    polled=(await client.get('/v1/connections/'+card['id'])).json()
    assert polled['status']=='ready' and 'verification_url' not in polled
    assert engine.backend.calls==[]


async def test_interrupted_connection_effect_is_not_replayed_after_restart(api):
    client,engine=api;card=await create(api,'scopus');manager=engine.connections
    row=manager._read(card['id']);manager._begin(row,'secret:api_key','interrupted-fixture',row['state_version'])
    fresh=ConnectionManager(engine,manager.browser)
    saved=fresh._read(card['id'])
    assert saved['status']=='needs_review' and saved['inflight'] is None
    assert saved['receipts']['interrupted-fixture']['status']=='interrupted_outcome_unknown'
    replay=await fresh.store_secret(card['id'],{'field':'api_key','value':'not-written-fixture',
        'expected_version':card['state_version'],'idempotency_key':'interrupted-fixture'})
    assert replay['status']=='needs_review' and engine.secrets.get('connection/'+card['id']+'/api_key') is None
