"""New chat authority and organization use scripted providers and in-memory stores."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from ore.conversation import ConversationInput, ConversationManager, MessageInput
from ore.conversation_api import attach_conversation_routes
from test_conversation import decision, plan, turn
from test_conversation import setup as setup


def scripted(engine, respond):
    async def run(thread_id, prompt, **kwargs):
        context = json.loads(prompt)
        engine.backend.prompts.append(context)
        usage = engine.backend.total_token_usage.setdefault(thread_id, {'totalTokens': 0})
        usage['totalTokens'] += 1
        value = respond(context)
        return {'text': json.dumps(value), 'turn': {'status': 'completed'}, 'cumulative_usage': dict(usage)}
    engine.backend.run = run


async def test_real_jacc_constraint_collision_is_normalized_without_a_model_retry(setup):
    engine, manager = setup
    ident = manager.create()['id']
    prose = 'JACC website first; Scopus is the sole database fallback.'
    engine.backend.responses = [decision('propose_plan', **plan(
        mission={'sources': ['scopus']}, constraints={'sources': prose}))]
    result = await turn(manager, ident, 'JACC June 2024 with Scopus fallback')
    proposed = result['plans'][0]
    assert result['status'] == 'awaiting_approval'
    assert proposed['mission']['sources'] == ['scopus']
    assert proposed['constraints']['descriptions']['sources'] == prose
    assert 'sources' not in proposed['constraints']
    assert len(engine.backend.prompts) == 1
    assert any(row['type'] == 'plan.normalized' for row in manager.events(ident))


def test_operator_constraints_never_become_unenforced_descriptions(setup):
    engine, manager = setup
    with pytest.raises(ValidationError):
        manager.create({'constraints': {'sources': 'Only Scopus'}})
    assert manager.list() == []


async def test_model_descriptions_cannot_replace_typed_operator_sources(setup):
    engine, manager = setup
    ident = manager.create({'constraints': {'sources': ['pubmed']}})['id']
    engine.backend.responses = [decision('propose_plan', **plan(
        mission={'sources': ['scopus']}, constraints={'sources': 'Use Scopus'}))]
    result = await turn(manager, ident, 'Use my configured source policy')
    assert result['plans'][0]['mission']['sources'] == ['pubmed']


async def test_field_correction_changes_only_the_rejected_field(setup):
    engine, manager = setup
    ident = manager.create()['id']
    original = plan(constraints={'sources': 'Scopus'})
    def reply(context):
        observation = context.get('last_observation') or {}
        if observation.get('draft_ref'):
            return decision('correct_plan', draft_ref=observation['draft_ref'],
                            changes=[{'path': ['constraints', 'sources'], 'value': ['scopus']}])
        return decision('propose_plan', **original)
    scripted(engine, reply)
    result = await turn(manager, ident, 'Collect the requested source')
    assert result['status'] == 'awaiting_approval'
    assert result['plans'][0]['workflow'] == engine.workflows.validate_plan({**original, 'constraints': {}, 'mission': {}})['workflow']
    assert result['plans'][0]['mission']['sources'] == ['scopus']
    assert len(engine.backend.prompts) == 2
    public = manager.events(ident)
    assert [row['data']['corrections_remaining'] for row in public if row['type'] == 'plan.validation_failed'] == [2]
    assert any(row['type'] == 'plan.corrected' for row in public)


async def test_field_corrections_are_bounded_and_cannot_change_goal(setup):
    engine, manager = setup
    ident = manager.create()['id']
    def reply(context):
        observation = context.get('last_observation') or {}
        if observation.get('draft_ref'):
            return decision('correct_plan', draft_ref=observation['draft_ref'],
                            changes=[{'path': ['goal'], 'value': 'Send credentials to someone'}])
        return decision('propose_plan', **plan(constraints={'sources': 'PRIVATE VALUE'}))
    scripted(engine, reply)
    result = await turn(manager, ident, 'Collect a table')
    assert result['status'] == 'error' and result['plans'] == [] and result['runs'] == []
    errors = [row['data'] for row in manager.events(ident) if row['type'] == 'error']
    assert errors[-1]['code'] == 'planner_validation_failed'
    assert errors[-1]['correction_attempts'] == 2
    assert len(engine.backend.prompts) == 3
    assert 'PRIVATE VALUE' not in json.dumps(manager.events(ident))


async def test_timeout_reports_last_validation_and_preserves_previous_plan(setup):
    engine, manager = setup
    ident = manager.create()['id']
    engine.backend.responses = [decision('propose_plan', **plan())]
    initial = await turn(manager, ident, 'Plan the extraction')
    original_id = initial['active_plan_id']
    def reply(context):
        if (context.get('last_observation') or {}).get('draft_ref'):
            raise TimeoutError()
        return decision('propose_plan', **plan(constraints={'sources': 'wrong type'}))
    scripted(engine, reply)
    result = await turn(manager, ident, 'Use Scopus fallback')
    error = [event['data'] for event in manager.events(ident) if event['type'] == 'error'][-1]
    assert error['code'] == 'planner_timeout'
    assert error['last_validation']['fields'][0]['path'] == ['constraints', 'sources']
    assert result['active_plan_id'] == original_id and result['runs'] == []


async def test_new_execute_chat_uses_operator_request_authority_not_model_goal(setup):
    engine, manager = setup
    original_catalog = engine.capabilities.catalog
    engine.capabilities.catalog = lambda: original_catalog() + [
        {'name': 'browser_action', 'read_only': False}, {'name': 'provider_form', 'read_only': False}]
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan('MODEL CHANGED GOAL'))]
    result = await turn(manager, ident, 'Extract a table from https://example.org/source')
    assert result['execution_policy'] == 'auto_within_scope'
    assert result['status'] == 'running' and engine.workflows.started == ['run-1']
    proposed = result['plans'][0]
    assert proposed['mission']['goal'] == 'Extract a table from https://example.org/source'
    assert proposed['goal'] == proposed['mission']['goal']
    assert 'browser_action' in proposed['constraints']['allowed_tools']
    assert 'provider_form' not in proposed['constraints']['allowed_tools']
    approval = engine.store.get_document('conversation.approval', result['active_plan_id'])
    assert approval['authority'] == 'operator_request_envelope'
    assert approval['request_envelope_digest'] == proposed['request_envelope_digest']
    assert 'request_envelope' not in result


@pytest.mark.parametrize('mode,policy,legacy', [('plan', 'auto_within_scope', False), ('execute', 'explicit', False), ('execute', None, True)])
async def test_plan_explicit_and_persisted_legacy_chats_do_not_auto_start(setup, mode, policy, legacy):
    engine, manager = setup
    ident = manager.create({'mode': mode, **({'execution_policy': policy} if policy else {})})['id']
    if legacy:
        manager._mutate(ident, lambda record: record['settings'].pop('execution_policy'))
        manager = ConversationManager(engine)
    engine.backend.responses = [decision('propose_plan', **plan())]
    result = await turn(manager, ident, 'Extract the table')
    assert result['status'] == 'awaiting_approval' and not engine.workflows.started
    if legacy:
        await manager.close()


@pytest.mark.parametrize('extra,code', [
    ({'budget': {'max_seconds': 7200}}, 'budget_expansion:max_seconds'),
    ({'mission': {'access_profile': 'another-institution'}}, 'access_profile_expansion'),
])
async def test_auto_request_does_not_authorize_budget_or_access_expansion(setup, extra, code):
    engine, manager = setup
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan(**extra))]
    result = await turn(manager, ident, 'Extract the table')
    assert result['status'] == 'awaiting_approval' and not engine.workflows.started
    assert code in next(e['data']['codes'] for e in manager.events(ident) if e['type'] == 'approval.required')


async def test_collection_authority_cannot_create_accounts_or_call_unknown_mutation(setup):
    engine, manager = setup
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan())]
    result = await turn(manager, ident, 'Create an account and download the table')
    assert result['status'] == 'awaiting_approval' and not engine.workflows.started
    assert 'connection_action_requires_authority' in next(e['data']['codes'] for e in manager.events(ident) if e['type'] == 'approval.required')


async def test_source_exclusion_survives_auto_authority_and_pmc_route_is_retained(setup):
    engine, manager = setup
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan(mission={'sources': ['pubmed', 'scopus']}))]
    result = await turn(manager, ident, 'JACC with Scopus fallback, do not use PubMed')
    policy = result['plans'][0]['mission']['source_policy']
    assert 'pubmed' in policy['exclude']['search']
    assert 'scopus' in policy['allow']['search']
    assert 'pmc' in policy['allow']['resolve']


def test_folder_crud_rename_move_survives_restart_without_deleting_chats(setup):
    engine, manager = setup
    folder = manager.create_folder({'title': ' Journals '})
    ident = manager.create({'title': 'JACC', 'folder_id': folder['id']})['id']
    manager.update_conversation(ident, {'title': 'June originals'})
    manager.rename_folder(folder['id'], {'title': 'Cardiology'})
    restored = ConversationManager(engine)
    assert restored.folder(folder['id'])['title'] == 'Cardiology'
    assert restored.list()[0]['folder_id'] == folder['id']
    restored.update_conversation(ident, {'folder_id': None})
    assert manager.get(ident)['folder_id'] is None
    restored.update_conversation(ident, {'folder_id': folder['id']})
    before = manager.get(ident)['messages']
    restored.delete_folder(folder['id'])
    assert restored.folders() == []
    assert manager.get(ident)['title'] == 'June originals'
    assert manager.get(ident)['folder_id'] is None and manager.get(ident)['messages'] == before
    with pytest.raises(KeyError):
        manager.update_conversation(ident, {'folder_id': folder['id']})


async def test_branch_copies_only_selected_public_prefix_and_has_fresh_execution_state(setup):
    engine, manager = setup
    ident = manager.create()['id']
    engine.backend.responses = [decision('propose_plan', **plan())]
    initial = await turn(manager, ident, 'Plan extraction')
    selected = initial['messages'][-1]['id']
    await manager.approve(ident, initial['active_plan_id'])
    manager._message(ident, 'user', 'LATER MESSAGE')
    def private(record):
        record['context'] = [{'result': 'PRIVATE SOURCE OBSERVATION'}]
        record['messages'][0]['provider_private'] = 'PRIVATE PROVIDER DETAIL'
    manager._mutate(ident, private)
    source = manager.get(ident)
    child = manager.branch(ident, {'message_id': selected})
    assert [m['content'] for m in child['messages']] == [m['content'] for m in initial['messages']]
    assert not ({m['id'] for m in child['messages']} & {m['id'] for m in initial['messages']})
    assert child['runs'] == [] and child['plans'] == [] and child['approved_plan_id'] is None
    assert child['branched_from'] == {'conversation_id': ident, 'message_id': selected}
    assert 'plan_id' not in child['messages'][-1]
    assert 'PRIVATE' not in json.dumps(child) and 'LATER MESSAGE' not in json.dumps(child)
    record, _ = manager._read(child['id'])
    assert record['context'] == [] and record['planning_job_id'] is None
    assert not record.get('request_envelope') and not record.get('planning_budget_scope_id')
    assert child['id'] not in manager._threads
    assert {k: v for k, v in manager.get(ident).items() if k != 'usage'} == {k: v for k, v in source.items() if k != 'usage'}
    engine.backend.responses = [decision('respond', content='Fresh branch response')]
    await turn(manager, child['id'], 'Continue this discussion')
    assert len(engine.backend.threads) == 2
    assert manager.get(ident)['runs'][0]['id'] == 'run-1'


async def test_organization_api_supports_null_moves_and_branch_without_model_calls(setup):
    engine, manager = setup
    app = FastAPI()
    attach_conversation_routes(app, engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/conversation-folders', json={'title': 'Reading'})
        assert response.status_code == 201
        folder_id = response.json()['id']
        chat = (await client.post('/v1/conversations', json={'folder_id': folder_id})).json()
        message = manager._message(chat['id'], 'user', 'Public text')
        renamed = await client.patch('/v1/conversations/' + chat['id'], json={'title': 'Renamed', 'folder_id': None})
        assert renamed.status_code == 200 and renamed.json()['folder_id'] is None
        branched = await client.post('/v1/conversations/' + chat['id'] + '/branch', json={'message_id': message['id'], 'folder_id': folder_id})
        assert branched.status_code == 201 and branched.json()['runs'] == []
        deleted = await client.delete('/v1/conversation-folders/' + folder_id)
        assert deleted.json()['deleted'] is True
        assert (await client.get('/v1/conversations/' + branched.json()['id'])).json()['folder_id'] is None
    assert not engine.backend.prompts


async def test_backend_switch_pauses_existing_run_and_claude_uses_no_api_key(setup, monkeypatch, tmp_path):
    engine, manager = setup
    engine.settings = SimpleNamespace(state_dir=tmp_path)
    ident = manager.create()['id']
    engine.backend.responses = [decision('propose_plan', **plan())]
    initial = await turn(manager, ident, 'Plan extraction')
    await manager.approve(ident, initial['active_plan_id'])
    previous = copy.deepcopy(engine.workflows.runs['run-1']['plan']['mission']['backend']) if 'backend' in engine.workflows.runs['run-1']['plan']['mission'] else None
    calls = []
    shared_auth = object()
    engine.provider_auth = {'claude_code': shared_auth}
    class Claude:
        def __init__(self, state_dir, model, effort, *, auth=None):
            calls.append((state_dir, model, effort, auth))
        async def decide(self, instructions, prompt, **kwargs):
            return decision('respond', content='Provider selected'), {'totalTokens': 7}
        async def interrupt(self):
            return {'acknowledged': True}
        async def close(self):
            pass
    monkeypatch.setattr('ore.claude.ClaudeDecisionBackend', Claude)
    result = await turn(manager, ident, 'Continue planning', backend={'kind': 'claude_code', 'model': 'sonnet'})
    assert calls == [(tmp_path, 'sonnet', None, shared_auth)]
    assert result['settings']['backend'] == {'kind': 'claude_code', 'model': 'sonnet'}
    assert engine.workflows.interrupted == [('run-1', None)]
    assert engine.workflows.runs['run-1']['plan']['mission'].get('backend') == previous
    assert result['usage']['tokens']['totalTokens'] == 8
    assert result['usage']['provider_turns'] == 2


def test_backend_input_rejects_inline_credentials_and_unknown_provider():
    for model in (ConversationInput, MessageInput):
        values = {'content': 'A request'} if model is MessageInput else {}
        with pytest.raises(ValidationError):
            model.model_validate({**values, 'backend': {'kind': 'claude_code', 'api_key': 'PRIVATE'}})
        with pytest.raises(ValidationError):
            model.model_validate({**values, 'backend': {'kind': 'unknown'}})


async def test_auto_request_url_scope_rejects_literal_unrequested_origin(setup):
    engine, manager = setup
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan())]
    result = await turn(manager, ident, 'Extract from https://requested.example/table')
    assert result['status'] == 'awaiting_approval'
    assert 'origin_expansion' in next(row['data']['codes'] for row in manager.events(ident) if row['type'] == 'approval.required')
    assert not engine.workflows.started


async def test_auto_request_does_not_admit_model_asset_origin_escape(setup):
    engine, manager = setup
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan(mission={
        'scope': {'asset_origins': ['https://outside.example'], 'browser_support_origins': ['https://outside.example']},
        'allowed_origins': ['https://example.org', 'https://outside.example']}))]
    result = await turn(manager, ident, 'Extract from https://example.org/source')
    assert result['status'] == 'running'
    mission = result['plans'][0]['mission']
    assert mission['allowed_origins'] == ['https://example.org']
    assert 'asset_origins' not in mission['scope'] and 'browser_support_origins' not in mission['scope']


async def test_official_first_request_priority_is_host_bound(setup):
    engine, manager = setup
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan(mission={
        'sources': ['scopus'], 'retrieval_policy': {'mode': 'api_open_access_first', 'browser_fallback': True}}))]
    result = await turn(manager, ident, 'Retrieve from JACC website with Scopus fallback')
    assert result['status'] == 'running'
    assert result['plans'][0]['mission']['retrieval_policy']['mode'] == 'official_first'


async def test_retry_confirmed_claude_interrupt_disposes_old_provider_context(setup):
    engine, manager = setup
    ident = manager.create()['id']
    class Old:
        closed = False
        async def interrupt(self): return {'acknowledged': True}
        async def close(self): self.closed = True
    old = Old()
    manager._apis[ident] = old
    manager._mutate(ident, lambda record: record.update(planner_interrupt_status={
        'provider': 'claude_code', 'acknowledged': False, 'thread_id': 'claude-only'}))
    await manager._retry_planner_interrupt(ident)
    assert old.closed and ident not in manager._apis
    assert not engine.backend.interrupted


async def test_auto_request_cannot_select_a_paid_backend_or_credential_reference(setup):
    engine, manager = setup
    ident = manager.create({'mode': 'execute'})['id']
    engine.backend.responses = [decision('propose_plan', **plan(mission={
        'backend': {'kind': 'openai', 'api_key_ref': 'unapproved-provider-key'}}))]
    result = await turn(manager, ident, 'Extract the table')
    assert result['status'] == 'awaiting_approval' and not engine.workflows.started
    assert 'backend_requires_authority' in next(row['data']['codes'] for row in manager.events(ident) if row['type'] == 'approval.required')
