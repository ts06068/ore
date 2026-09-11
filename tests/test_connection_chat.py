"""Connection bootstrap from chat without a configured language model."""
import json
from unittest.mock import AsyncMock
import pytest
from ore.connection_intent import connection_setup_intent, has_configured_api_key
from ore.credentials import CredentialPool
from ore.source_policy import source_readiness
from test_connections import api, create, action
from test_engine import engine


@pytest.mark.parametrize('content,providers', [
    ('Connect Codex', ['codex']), ('Claude 로그인해줘', ['claude_code']),
    ('Scopus API 발급해줘', ['scopus']), ('Scopus와 PubMed 연결해줘', ['scopus','pubmed']),
    ('Connect CrossRef and Clarivate', ['crossref','wos']), ('OpenAI API key 입력', ['openai']),
    ('Enter my Anthropic API key', ['anthropic']), ('PubMed\nAPI key 발급해줘', ['pubmed']),
    ('Connect ChatGPT API', ['openai']), ('Connect Claude API', ['anthropic']),
    ('Connect ChatGPT and Claude API', ['codex', 'anthropic']),
    ('Connect ChatGPT API and Claude sign in', ['openai', 'claude_code']),
    ('Connect ChatGPT, Claude API', ['codex', 'anthropic']),
    ('Connect Claude API and Codex', ['anthropic', 'codex']),
    ('Connect Codex API', ['codex']),
    ('Connect ChatGPT API and ChatGPT login', ['openai', 'codex']),
])
def test_explicit_setup_commands(content, providers):
    assert connection_setup_intent(content)['providers'] == providers


@pytest.mark.parametrize('content', [
    "Find the original articles in JACC's June 2024 issues - retrieve from the JACC website with Scopus fallback.",
    'Connect Scopus and collect five cardiac death papers', 'PubMed에서 original research 5편 수집해줘',
    'How does Scopus index articles?', 'Scopus', 'Reconnect my unrelated service',
])
def test_collection_and_unrelated_messages_stay_with_planner(content):
    assert connection_setup_intent(content) is None


async def test_model_login_from_chat_without_model_or_jobs(api):
    client, engine = api
    conversation = engine.conversations.create({})
    response = await client.post(f"/v1/conversations/{conversation['id']}/messages", json={'content':'Codex 로그인해줘'})
    assert response.status_code == 202, response.text
    result = response.json()
    assert not result['planner_running'] and result['plans'] == []
    assert engine.backend.calls == [] and engine.store.list_jobs() == []
    cards = engine.connections.list(conversation['id'])
    assert len(cards) == 1 and cards[0]['status'] == 'awaiting_auth'
    assert 'FIXTURE-CODE' not in json.dumps(result)
    assert 'FIXTURE-CODE' not in json.dumps(engine.store.get_document('conversation',conversation['id']))


async def test_setup_preserves_active_plan_and_provider_settings(api):
    client, engine = api
    conversation = engine.conversations.create({})
    ident = conversation['id']
    engine.conversations._mutate(ident, lambda value: value.update(status='running',
        last_planner_request={'content':'Original collection request'}, request_envelope={'original':True}))
    engine.conversations._cancel_planner = AsyncMock()
    response = await client.post(f'/v1/conversations/{ident}/messages', json={
        'content':'Connect Crossref', 'backend':{'kind':'anthropic'}, 'model':'unused'})
    assert response.status_code == 202, response.text
    row, _ = engine.conversations._read(ident)
    assert row['status'] == 'running' and row['last_planner_request'] == {'content':'Original collection request'}
    assert row['request_envelope'] == {'original':True}
    assert row['settings'].get('backend', {'kind':'codex'})['kind'] == 'codex'
    engine.conversations._cancel_planner.assert_not_awaited()
    assert 'no account or API key' in response.json()['messages'][-1]['content']
    assert engine.connections.list(ident)[0]['credential_fields'] == ['email']


async def test_inline_setup_credentials_are_omitted_not_sent_to_model(api):
    client, engine = api
    conversation = engine.conversations.create({})
    response = await client.post(f"/v1/conversations/{conversation['id']}/messages", json={
        'content':'Connect Scopus, password: synthetic-sensitive-phrase'})
    assert response.status_code == 202, response.text
    assert 'synthetic-sensitive-phrase' not in response.text
    assert 'synthetic-sensitive-phrase' not in json.dumps(engine.store.get_document('conversation',conversation['id']))
    assert engine.backend.calls == []


async def test_pending_api_is_reused_without_new_enrollment(api):
    client, engine = api
    engine.save_profile({'id':'pending','source_readiness':{'wos':{'search':{'state':'approval_pending'}}}})
    conversation = engine.conversations.create({'constraints':{'access_profile_ref':'pending'}})
    response = await client.post(f"/v1/conversations/{conversation['id']}/messages", json={'content':'Clarivate API 발급해줘'})
    assert response.status_code == 202, response.text
    cards = engine.connections.list(conversation['id'])
    assert cards[0]['status'] == 'approval_pending'
    assert engine.store.list_jobs() == [] and engine.backend.calls == []


async def test_multiple_keys_keep_shared_quota_and_private_values(api):
    client, engine = api
    card = await create(api, 'scopus')
    for ident in ('primary','secondary'):
        response = await client.post(f"/v1/connections/{card['id']}/secret", json={
            'field':'api_key','credential_id':ident,'value':'synthetic-secret-'+ident,
            'expected_version':card['state_version'],'idempotency_key':'pool-key-'+ident})
        assert response.status_code == 200, response.text
        assert 'synthetic-secret-' not in response.text
        card = response.json()
    assert [entry['id'] for entry in card['credential_pool']] == ['primary','secondary']
    profile = engine.profile({'access_profile_ref':card['access_profile_ref']})
    pool = CredentialPool(engine.store, profile, engine.secrets.get)
    assert {entry['quota_group'] for entry in pool.entries('scopus')} == {'shared:scopus'}
    assert source_readiness('scopus','search',profile)['configured']
    pool_profile = {'sources':{'scopus':{'credential_pool':profile['sources']['scopus']['credential_pool']}}}
    assert source_readiness('scopus','search',pool_profile)['configured']


async def test_pubmed_rejects_extra_key_slot_and_crossref_accepts_email(api):
    client, engine = api
    card = await create(api, 'pubmed')
    response = await client.post(f"/v1/connections/{card['id']}/secret", json={
        'field':'api_key','credential_id':'secondary','value':'synthetic-key',
        'expected_version':card['state_version'],'idempotency_key':'ncbi-extra-key'})
    assert response.status_code == 422
    assert not engine.connections.public(engine.connections._read(card['id']))['configured_fields']
    card = await create(api, 'crossref')
    response = await client.post(f"/v1/connections/{card['id']}/secret", json={
        'field':'email','value':'operator@example.test',
        'expected_version':card['state_version'],'idempotency_key':'contact-email'})
    assert response.status_code == 200, response.text
    assert 'operator@example.test' not in response.text


async def test_selected_source_profile_reaches_next_plan_without_reusing_old_recon(api):
    client, engine = api
    engine.save_profile({'id':'connected-scopus','sources':{'scopus':{'api_key_ref':'fixture/scopus'}}})
    conversation = engine.conversations.create({'mode':'execute'})
    ident = conversation['id']
    await client.post(f'/v1/conversations/{ident}/messages', json={'content':'Connect Crossref'})
    engine.conversations._mutate(ident, lambda value: value.update(planning_job_id='old-recon', context=[{'old':True}]))
    engine.conversations._plan_turn = AsyncMock()
    response = await client.post(f'/v1/conversations/{ident}/messages', json={
        'content':'Find five cardiac death articles in Scopus', 'access_profile_ref':'connected-scopus'})
    assert response.status_code == 202, response.text
    record, _ = engine.conversations._read(ident)
    assert record['settings']['constraints']['access_profile_ref'] == 'connected-scopus'
    assert record['planning_job_id'] is None and record['context'] == []
    assert record['request_envelope']['access_profile'] == 'connected-scopus'
    assert record['request_envelope']['goal'] == 'Find five cardiac death articles in Scopus'
    assert 'crossref' not in record['request_envelope']['source_order']


async def test_unknown_profile_is_rejected_before_existing_planner_interruption(api):
    client, engine = api
    conversation = engine.conversations.create({})
    engine.conversations._cancel_planner = AsyncMock()
    response = await client.post(f"/v1/conversations/{conversation['id']}/messages", json={
        'content':'Find five cardiac death papers', 'access_profile_ref':'unknown-profile'})
    assert response.status_code == 403
    engine.conversations._cancel_planner.assert_not_awaited()


async def test_verify_performs_one_bounded_search_and_persists_profile_evidence(api, monkeypatch):
    client, engine = api
    engine.secrets.set('fixture/verify-key', 'synthetic-verify-value')
    engine.save_profile({'id': 'verify-profile', 'sources': {'scopus': {'api_key_ref': 'fixture/verify-key'}}})
    search = AsyncMock(return_value=[])
    monkeypatch.setattr('ore_scholarly.search', search)
    card = await create(api, 'scopus', access_profile_ref='verify-profile')
    request = action(card, 'verify', 'verify-once')
    response = await client.post(f"/v1/connections/{card['id']}/actions", json=request)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ready'
    assert response.json()['message_code'] == 'source_operation_checked'
    assert 'synthetic-verify-value' not in response.text
    search.assert_awaited_once()
    assert search.call_args.args == ('scopus', 'cardiology')
    assert search.call_args.kwargs['limit'] == 1
    assert search.call_args.kwargs['config']['api_key_ref'] == 'fixture/verify-key'
    saved = source_readiness('scopus', 'search', engine.profile({'access_profile_ref': 'verify-profile'}))
    assert saved['state'] == 'ready' and saved['evidence_ref'].startswith('source_check:')
    replay = await client.post(f"/v1/connections/{card['id']}/actions", json=request)
    assert replay.status_code == 200 and replay.json()['status'] == 'ready'
    search.assert_awaited_once()
    assert engine.backend.calls == [] and engine.store.list_jobs() == []


async def test_verify_records_provider_denial_without_echoing_private_error(api, monkeypatch):
    from ore.source_policy import SourceUnavailable
    client, engine = api
    engine.save_profile({'id': 'denied-profile', 'sources': {'scopus': {'api_key_ref': 'fixture/denied-key'}}})
    search = AsyncMock(side_effect=SourceUnavailable({'code': 'entitlement_denied', 'reason': 'synthetic-private-provider-error'}))
    monkeypatch.setattr('ore_scholarly.search', search)
    card = await create(api, 'scopus', access_profile_ref='denied-profile')
    response = await client.post(f"/v1/connections/{card['id']}/actions", json=action(card, 'verify', 'verify-denied'))
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'entitlement_denied'
    assert 'synthetic-private-provider-error' not in response.text
    assert source_readiness('scopus', 'search', engine.profile({'access_profile_ref': 'denied-profile'}))['state'] == 'entitlement_denied'
    assert all('synthetic-private-provider-error' not in json.dumps(row) for row in engine.store.list_documents('source_check'))
    assert engine.backend.calls == []


async def test_verify_keeps_pending_without_credentials_and_performs_no_io(api, monkeypatch):
    client, engine = api
    monkeypatch.delenv('WOS_API_KEY', raising=False)
    engine.save_profile({'id': 'still-pending', 'source_readiness': {'wos': {'search': {'state': 'approval_pending'}}}})
    check = AsyncMock()
    monkeypatch.setattr('ore.onboarding.check_source', check)
    card = await create(api, 'wos', access_profile_ref='still-pending')
    response = await client.post(f"/v1/connections/{card['id']}/actions", json=action(card, 'verify', 'pending-no-check'))
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'approval_pending'
    assert response.json()['message_code'] == 'pending_operation_excluded_from_admission'
    check.assert_not_awaited()
    assert engine.backend.calls == [] and engine.store.list_jobs() == []


async def test_verify_search_does_not_silently_validate_a_browser_connection(api, monkeypatch):
    client, engine = api
    check = AsyncMock()
    monkeypatch.setattr('ore.onboarding.check_source', check)
    card = await create(api, 'scopus', operation='browser')
    assert 'verify' not in card['actions']
    response = await client.post(f"/v1/connections/{card['id']}/actions", json=action(card, 'verify', 'not-search-check'))
    assert response.status_code == 403
    check.assert_not_awaited()
    assert engine.backend.calls == []


async def test_existing_scopus_key_bootstrap_avoids_a_second_account_workflow(api):
    client, engine = api
    engine.secrets.set('fixture/existing-key', 'synthetic-existing-key')
    engine.save_profile({'id': 'existing-api', 'sources': {'scopus': {'api_key_ref': 'fixture/existing-key'}}})
    conversation = engine.conversations.create({})
    response = await client.post(f"/v1/conversations/{conversation['id']}/messages", json={
        'content': 'Scopus API 발급해줘', 'access_profile_ref': 'existing-api'})
    assert response.status_code == 202, response.text
    assert 'already configured' in response.json()['messages'][-1]['content']
    cards = engine.connections.list(conversation['id'])
    assert cards[0]['access_profile_ref'] == 'existing-api' and 'verify' in cards[0]['actions']
    assert engine.store.list_jobs() == [] and engine.backend.calls == []
    assert engine.provider_auth['codex'].starts == 0


@pytest.mark.parametrize('config,expected', [
    ({}, True),
    ({'api_key_env': 'ORE_FIXTURE_SOURCE_KEY'}, True),
    ({'api_key_ref': 'fixture/missing-key'}, False),
    ({'api_key_ref': 'fixture/present-key'}, True),
    ({'credential_pool': [{'id': 'off', 'api_key_ref': 'fixture/present-key', 'enabled': False}], 'api_key_ref': 'fixture/present-key'}, False),
    ({'credential_pool': [{'id': 'on', 'api_key_ref': 'fixture/present-key', 'enabled': True}]}, True),
    ({'credential_pool': []}, False),
])
def test_existing_api_key_detection_respects_refs_env_and_disabled_pools(engine, monkeypatch, config, expected):
    monkeypatch.setenv('SCOPUS_API_KEY', 'synthetic-environment-key')
    monkeypatch.setenv('ORE_FIXTURE_SOURCE_KEY', 'synthetic-custom-environment-key')
    engine.secrets.set('fixture/present-key', 'synthetic-present-key')
    assert has_configured_api_key(engine, 'scopus', {'sources': {'scopus': config}}) is expected


async def test_verify_preserves_concurrent_profile_update_and_requires_reverification(api, monkeypatch):
    client, engine = api
    engine.secrets.set('fixture/old-key', 'synthetic-old-key')
    engine.secrets.set('fixture/new-key', 'synthetic-new-key')
    engine.save_profile({'id': 'changing-profile', 'sources': {'scopus': {'api_key_ref': 'fixture/old-key'}}})
    async def update_during_search(*args, **kwargs):
        profile = engine.profile({'access_profile_ref': 'changing-profile'})
        profile['sources']['scopus']['api_key_ref'] = 'fixture/new-key'
        profile['source_policy'] = {'exclude': {'search': ['crossref']}}
        engine.save_profile(profile)
        return []
    monkeypatch.setattr('ore_scholarly.search', AsyncMock(side_effect=update_during_search))
    card = await create(api, 'scopus', access_profile_ref='changing-profile')
    response = await client.post(f"/v1/connections/{card['id']}/actions", json=action(card, 'verify', 'changing-source-check'))
    assert response.status_code == 200, response.text
    profile = engine.profile({'access_profile_ref': 'changing-profile'})
    assert profile['sources']['scopus']['api_key_ref'] == 'fixture/new-key'
    assert profile['source_policy'] == {'exclude': {'search': ['crossref']}}
    assert response.json()['status'] != 'ready'
    assert source_readiness('scopus', 'search', profile)['state'] != 'ready'


async def test_planning_reconnaissance_retains_original_goal_after_connection_message(api):
    client, engine = api
    conversation = engine.conversations.create({})
    original = 'Find original cardiac death articles in PubMed'
    engine.conversations._mutate(conversation['id'], lambda value: value.update(last_planner_request={'content': original}))
    response = await client.post(f"/v1/conversations/{conversation['id']}/messages", json={'content': 'Connect Crossref'})
    assert response.status_code == 202, response.text
    engine.conversations._planning_runtime(conversation['id'])
    record, _ = engine.conversations._read(conversation['id'])
    assert engine.store.get_job(record['planning_job_id'])['mission']['goal'] == original


async def test_storing_replacement_api_key_invalidates_previous_verification(api, monkeypatch):
    client, engine = api
    monkeypatch.setattr('ore_scholarly.search', AsyncMock(return_value=[]))
    card = await create(api, 'scopus')
    first = await client.post(f"/v1/connections/{card['id']}/secret", json={
        'field': 'api_key', 'value': 'synthetic-first-key', 'expected_version': card['state_version'],
        'idempotency_key': 'first-verification-key'})
    card = first.json()
    verified = await client.post(f"/v1/connections/{card['id']}/actions", json=action(card, 'verify', 'before-rotation-check'))
    card = verified.json()
    assert card['status'] == 'ready'
    replacement = await client.post(f"/v1/connections/{card['id']}/secret", json={
        'field': 'api_key', 'value': 'synthetic-replacement-key', 'expected_version': card['state_version'],
        'idempotency_key': 'replacement-verification-key'})
    assert replacement.status_code == 200, replacement.text
    profile = engine.profile({'access_profile_ref': replacement.json()['access_profile_ref']})
    assert replacement.json()['status'] != 'ready'
    assert source_readiness('scopus', 'search', profile)['state'] != 'ready'


async def test_verify_detects_secret_rotation_even_when_reference_is_unchanged(api, monkeypatch):
    client, engine = api
    ref = 'fixture/shared-verify-reference'
    engine.secrets.set(ref, 'synthetic-before-check')
    engine.save_profile({'id': 'same-reference-profile', 'sources': {'scopus': {'api_key_ref': ref}}})
    async def rotate_during_search(*args, **kwargs):
        engine.secrets.set(ref, 'synthetic-after-check')
        return []
    monkeypatch.setattr('ore_scholarly.search', AsyncMock(side_effect=rotate_during_search))
    card = await create(api, 'scopus', access_profile_ref='same-reference-profile')
    response = await client.post(f"/v1/connections/{card['id']}/actions", json=action(card, 'verify', 'same-ref-source-check'))
    assert response.status_code == 200, response.text
    assert response.json()['status'] != 'ready'
    assert source_readiness('scopus', 'search', engine.profile({'access_profile_ref': 'same-reference-profile'}))['state'] != 'ready'
    assert engine.secrets.get(ref) == 'synthetic-after-check'
    assert 'synthetic-before-check' not in response.text and 'synthetic-after-check' not in response.text
