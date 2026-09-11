"""Native workflow integration with real fenced storage and scripted sessions."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore.agent_sessions import ToolCallContext, ToolResult
from ore.models import canonical_digest
from ore.run_budget import RunBudget
from ore.workflow import WorkflowManager
from ore.workflow_context import INITIAL_BYTES, encoded, initial_context, slice_json
from test_workflow import engine, one, drain, node


class ScriptedSession:
    """No model judgments: scripts drive real native workflow handler contracts."""
    def __init__(self, **values):
        self.__dict__.update(values)
        self.thread_id = 'thread-' + canonical_digest(self.session_key)[:16]
        self.session_id = self.thread_id
        self.usage_baseline = {}
        self.yield_reason = None
        self.calls = 0

    async def start_or_resume(self, **kwargs):
        await self.authorize('start', None)
        self.backend.sessions.append(self)
        return self.thread_id

    def request_yield(self, reason): self.yield_reason = reason

    async def call(self, name, args, *, call_id=None):
        if self.yield_reason: return {'error': True, 'code': 'agent_yielding'}
        self.calls += 1
        ctx = ToolCallContext('codex', self.session_id, self.thread_id,
                              f'turn-{self.backend.rounds[self.node_id]}', call_id or f'call-{self.calls}', name, name)
        await self.authorize('tool', ctx)
        result = await self.handler(ctx, args)
        self.backend.results.append(result)
        return result.data if isinstance(result, ToolResult) else result

    async def run(self, prompt, **kwargs):
        await self.authorize('model', None)
        self.backend.timeouts.append(kwargs['timeout'])
        self.backend.prompts.append(json.loads(prompt))
        self.backend.rounds[self.node_id] = self.backend.rounds.get(self.node_id, 0) + 1
        await self.backend.script(self, json.loads(prompt))
        if not getattr(self.backend, 'suppress_usage', False):
            total = self.backend.usage_totals.get(self.thread_id, 0) + 1
            self.backend.usage_totals[self.thread_id] = total
            await self.on_usage({'totalTokens': total, 'provider': 'codex', 'session_id': self.session_id})
        return {'settled': True, 'control_yield': self.yield_reason, 'turn': {'status': 'completed'}}

    async def interrupt(self): return {'acknowledged': True}


@pytest.fixture
def native_engine(engine, monkeypatch):
    monkeypatch.setattr('ore.workflow_native.AgentSession', ScriptedSession)
    engine.backend = SimpleNamespace(sessions=[], prompts=[], rounds={}, results=[], usage_totals={}, timeouts=[])
    engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'fixture', 'effort': 'high', 'mode': 'fixed'}
    return engine


def plan(nodes, **extra):
    return {'goal': 'Complete the bounded fixture', 'workflow': {'nodes': nodes}, **extra}


def agent(checks=None, **extra):
    return {'id': 'parent', 'kind': 'agent', 'checks': checks or [
        {'op': 'eq', 'left': {'$ref': 'output.done'}, 'right': True}], **extra}


async def test_delegate_resumes_same_parent_and_distinct_groups(native_engine):
    engine = native_engine
    async def script(session, prompt):
        iteration = engine.backend.rounds['parent']
        if iteration <= 2:
            if iteration == 2:
                assert prompt['continuation']['reason'] == 'delegated_children_settled'
                assert prompt['continuation']['children'][0]['output'] == {'value': 1}
            await session.call('workflow.delegate', {'nodes': [node('same', {'value': iteration})]})
        else:
            assert prompt['continuation']['children'][0]['output'] == {'value': 2}
            await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    assert run['schema_version'] == 'ore.workflow/v2'
    await engine.workflows.start_run(run['id'])
    await one(engine, run)
    assert next(n for n in engine.workflows.nodes(run['id']) if n['id'] == 'parent')['status'] == 'waiting_children'
    result = await drain(engine, run)
    assert result['status'] == 'completed'
    assert len({s.thread_id for s in engine.backend.sessions}) == 1
    assert all(timeout > 600 for timeout in engine.backend.timeouts)
    assert engine.capabilities.calls == [('echo', {'value': 1}), ('echo', {'value': 2})]
    ids = {n['id'] for n in engine.workflows.nodes(run['id'])}
    assert {'parent/group-1/same', 'parent/group-2/same'} <= ids
    parent = next(n for n in engine.workflows.nodes(run['id']) if n['id'] == 'parent')
    assert parent['output'] == {'done': True}
    assert parent['completion_evidence']['validation_strength']['corpus'] is False


async def test_failed_child_returns_error_to_parent_without_whole_plan_repair(native_engine):
    engine = native_engine
    async def broken(args, runtime): raise ValueError('fixture failure')
    engine.capabilities.handlers['broken'] = broken
    async def script(session, prompt):
        if engine.backend.rounds['parent'] == 1:
            await session.call('workflow.delegate', {'nodes': [node('fail', tool='broken')]})
        else:
            child = prompt['continuation']['children'][0]
            assert child['status'] == 'needs_replan' and child['error']['code'] == 'ValueError'
            await session.call('echo', {'value': 'recovered'})
            await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    assert engine.workflows.get_run(run['id'])['plan_revision'] == 1


async def test_finish_correction_keeps_native_context_and_does_not_repeat_effect(native_engine):
    engine = native_engine
    async def script(session, prompt):
        await session.call('echo', {'value': 7})
        invalid = await session.call('workflow.finish', {'output': {'done': False}})
        assert invalid['error'] and invalid['corrections_remaining'] == 2
        valid = await session.call('workflow.finish', {'output': {'done': True}})
        assert valid['accepted']
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent(completion={'required_tools': [{'tool': 'echo', 'min_count': 1,
        'checks': [{'op': 'eq', 'left': {'$ref': 'output.value'}, 'right': 7}]}]})]))
    await engine.workflows.start_run(run['id'])
    result = await drain(engine, run)
    assert result['status'] == 'completed'
    assert engine.capabilities.calls == [('echo', {'value': 7})]
    assert len(engine.backend.sessions) == 1
    saved = next(n for n in engine.workflows.nodes(run['id']) if n['id'] == 'parent')
    assert saved['finish_validation_failures'] == 1
    assert saved['completion_evidence']['validation_strength']['independent_goal'] is True


async def test_finish_three_invalid_outputs_pause_only_node(native_engine):
    engine = native_engine
    async def script(session, prompt):
        await session.call('echo', {'value': 7})
        for remaining in (2, 1, 0):
            result = await session.call('workflow.finish', {'output': {'done': False}})
            assert result['corrections_remaining'] == remaining
        assert (await session.call('echo', {'value': 8}))['code'] == 'agent_yielding'
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    result = await drain(engine, run)
    assert result['status'] == 'awaiting_user'
    assert result['plan_revision'] == 1
    assert engine.capabilities.calls == [('echo', {'value': 7})]


async def test_schema_assertions_without_receipts_cannot_claim_completion(native_engine):
    engine = native_engine
    async def script(session, prompt):
        for _ in range(3):
            result = await session.call('workflow.finish', {'output': {'done': True}})
            assert result['code'] == 'receipt_evidence_missing'
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'awaiting_user'


async def test_parallel_native_calls_have_distinct_receipts_and_shared_cap(native_engine):
    engine = native_engine; active = peak = 0
    engine.settings.max_workers = 2
    async def echo(args, runtime):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
        return args
    engine.capabilities.handlers['echo'] = echo
    async def script(session, prompt):
        await asyncio.gather(*(session.call('echo', {'value': index}) for index in range(8)))
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    receipts = engine.store.list_documents('workflow.operation', run['job_id'])
    assert peak == 2 and len(receipts) == 8
    assert len({row['operation_id'] for row in receipts}) == 8


async def test_receipt_replay_same_native_call_does_not_repeat_effect(native_engine):
    engine = native_engine
    async def script(session, prompt):
        assert await session.call('echo', {'value': 4}, call_id='stable') == {'value': 4}
        assert await session.call('echo', {'value': 4}, call_id='stable') == {'value': 4}
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    assert engine.capabilities.calls == [('echo', {'value': 4})]


async def test_budget_exhaustion_stops_execution_without_repair(native_engine):
    engine = native_engine
    async def script(session, prompt):
        await session.call('echo', {'value': 4})
        await session.on_usage({'provider': 'codex', 'session_id': session.session_id, 'totalTokens': 101})
        pytest.fail('Exhausted usage must stop the session')
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()], budget={'max_tokens': 100}))
    await engine.workflows.start_run(run['id'])
    result = await drain(engine, run)
    assert result['status'] == 'paused_budget'
    assert result['plan_revision'] == 1 and len(engine.capabilities.calls) == 1
    assert RunBudget(engine.store, run['job_id']).snapshot()['token_overshoot'] == 1


def test_initial_context_is_bounded_and_does_not_repeat_full_plan():
    node = {'id': 'a', 'spec': {'inputs': {'huge': '가' * 100000}, 'checks': []}}
    run = {'id': 'run', 'plan': {'goal': 'fixture', 'private_unused': 'x' * 1000000}}
    payload = initial_context(run, node, {'goal': 'fixture'}, [])
    assert len(encoded(payload)) <= INITIAL_BYTES
    assert payload['inputs']['inspect'] == {'kind': 'inputs', 'node_id': 'a'}
    assert 'private_unused' not in str(payload) and 'plan' not in payload
    chunk = slice_json({'value': '가' * 20000}, limit=1000)
    assert len(chunk['json_text'].encode()) <= 1000 and chunk['next_offset']


async def test_recipe_program_and_promoted_warm_replay_use_fenced_operations(native_engine):
    engine = native_engine
    digest = 'a' * 64
    def verifier(inputs, evidence):
        values = [row['output']['value'] for row in evidence['receipts'] if row['tool'] == 'echo' and row['status'] == 'completed']
        passed = values == [inputs['value']] and evidence['result']['value'] == inputs['value']
        return {'passed': passed, 'validator_digest': digest, 'case_digest': canonical_digest({'observed': values})}
    engine.recipe_verifiers = {'echo-integrity': {'digest': digest, 'verify': verifier}}
    program = {'version': 1, 'steps': [{'id': 'source', 'tool': 'echo', 'inputs': {'value': {'$ref': 'inputs.value'}}}],
               'output': {'done': True, 'value': {'$ref': 'steps.source.value'}}}
    async def script(session, prompt):
        for value in range(5):
            result = await session.call('recipe.execute', {'program': program, 'inputs': {'value': value}})
        assert result['reuse_eligible'] is True
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    cold = engine.workflows.create_run(plan([agent(completion={'validator': 'echo-integrity'})]))
    await engine.workflows.start_run(cold['id'])
    assert (await drain(engine, cold))['status'] == 'completed'
    session_count = len(engine.backend.sessions)
    engine.backend.script = lambda *args: pytest.fail('Promoted replay must use no model')
    warm = engine.workflows.create_run(plan([agent(completion={'validator': 'echo-integrity'},
        replay={'program': program, 'inputs': {'value': 9}})]))
    await engine.workflows.start_run(warm['id'])
    result = await drain(engine, warm)
    assert result['status'] == 'completed'
    assert len(engine.backend.sessions) == session_count
    assert result['usage']['zero_model_calls'] is True
    assert result['reuse']['verified_reuses'] == 1
    assert engine.capabilities.calls[-1] == ('echo', {'value': 9})


async def test_unverified_recipe_hint_falls_back_to_native(native_engine):
    engine = native_engine
    program = {'version': 1, 'steps': [{'id': 'source', 'tool': 'echo', 'inputs': {'value': 1}}], 'output': {'done': True}}
    async def script(session, prompt):
        assert prompt['prior_execution']['reason'] == 'recipe_not_promoted'
        await session.call('echo', {'value': 8})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent(replay={'program': program, 'inputs': {}})]))
    await engine.workflows.start_run(run['id'])
    result = await drain(engine, run)
    assert result['status'] == 'completed'
    assert engine.capabilities.calls == [('echo', {'value': 8})]
    assert result['reuse']['verified_reuses'] == 0


async def test_versioned_recipe_node_runs_without_model(native_engine):
    engine = native_engine
    program = {'version': 1, 'steps': [{'id': 'source', 'tool': 'echo', 'inputs': {'value': {'$ref': 'inputs.value'}}}],
               'output': {'$ref': 'steps.source.value'}}
    run = engine.workflows.create_run(plan([{'id': 'deterministic', 'kind': 'recipe', 'program': program,
        'inputs': {'value': 3}, 'checks': [{'op': 'eq', 'left': {'$ref': 'output'}, 'right': 3}]}]))
    await engine.workflows.start_run(run['id'])
    result = await drain(engine, run)
    assert result['status'] == 'completed' and result['execution_runtime'] == 'deterministic'
    assert result['usage']['zero_model_calls'] is True and engine.backend.sessions == []


async def test_inspection_cannot_read_unrelated_sibling(native_engine):
    engine = native_engine
    async def script(session, prompt):
        denied = await session.call('workflow.inspect', {'kind': 'node', 'node_id': 'other'})
        assert denied['error'] and denied['code'] == 'AccessDenied'
        await session.call('echo', {'value': 8})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent(), node('other', {'secret': 'unrelated'})]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'


async def test_nested_child_failure_returns_to_native_parent(native_engine):
    engine = native_engine
    async def broken(args, runtime): raise ValueError('nested fixture failure')
    engine.capabilities.handlers['broken'] = broken
    async def script(session, prompt):
        if engine.backend.rounds['parent'] == 1:
            await session.call('workflow.delegate', {'nodes': [{'id': 'loop', 'kind': 'foreach', 'items': [1, 2],
                'body': [node('broken', tool='broken'), node('dependent', depends_on=['broken'])]}]})
        else:
            assert prompt['continuation']['children'][0]['error']['code'] == 'child_execution_failed'
            await session.call('echo', {'value': 'recovered'})
            await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    assert len(engine.backend.sessions) == 2


async def test_metadata_policy_filters_native_results_images_and_inspection(native_engine):
    from ore.engine import Engine
    engine = native_engine
    engine.model_observation = lambda mission, payload: Engine.model_observation(engine, mission, payload)
    async def source(args, runtime):
        runtime.last_image = 'data:image/png;base64,cHJpdmF0ZQ=='
        return {'status': 200, 'text': 'private body', 'body_base64': 'cHJpdmF0ZQ==',
                'values': ['private extracted text'], 'nested': {'body_base64': 'cHJpdmF0ZQ=='}, 'title': 'Metadata title'}
    engine.capabilities.handlers['source'] = source
    async def script(session, prompt):
        result = await session.call('source', {})
        assert result['title'] == 'Metadata title' and result['status'] == 200
        assert 'private' not in json.dumps(result) and 'cHJp' not in json.dumps(result)
        assert not engine.backend.results[-1].images
        inspected = await session.call('workflow.inspect', {'kind': 'receipts'})
        assert 'private' not in inspected['json_text'] and 'cHJp' not in inspected['json_text']
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()], external_model_content='metadata'))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    receipts = engine.store.list_documents('workflow.operation', run['job_id'])
    assert receipts[0]['output']['text'] == 'private body'


async def test_large_native_tool_result_is_bounded_and_inspectable(native_engine):
    engine = native_engine
    async def source(args, runtime): return {'text': '가나다' * 30000, 'status': 200}
    engine.capabilities.handlers['source'] = source
    async def script(session, prompt):
        result = await session.call('source', {})
        assert result['stored'] and len(encoded(result)) < 64 * 1024
        inspected = await session.call('workflow.inspect', {**result['inspect'], 'limit': 2000})
        assert len(inspected['json_text'].encode()) <= 2000 and inspected['next_offset'] is not None
        assert '가나다' in inspected['json_text']
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'


async def test_native_interrupt_retry_routes_to_dedicated_backend(native_engine):
    engine = native_engine
    child = SimpleNamespace(turn_ids={}, turn_completions={}, start=AsyncMock(),
        request=AsyncMock(return_value={'thread': {'turns': [{'id': 'native-turn', 'status': 'interrupted'}]}}),
        interrupt=AsyncMock(return_value={'thread_id': 'native-thread', 'turn_id': 'native-turn', 'acknowledged': True}),
        settle_tools=AsyncMock(return_value={'acknowledged': True, 'pending': 0}))
    engine.backend.scoped_native_backend = lambda: child
    run = engine.workflows.create_run(plan([agent()]))
    result = await engine.workflows.native.retry_interrupt(run, {'thread_id': 'native-thread', 'turn_id': 'native-turn'})
    assert result['acknowledged'] and result['provider_runtime'] == 'native'
    child.request.assert_awaited_once_with('thread/read', {'threadId': 'native-thread', 'includeTurns': True}, timeout=10)
    assert child.turn_completions[('native-thread', 'native-turn')].is_set()


async def test_completed_budget_clock_stops_and_shared_new_run_keeps_clock_active(native_engine):
    engine = native_engine
    async def script(session, prompt):
        await session.call('echo', {'value': 1})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    shared = 'shared-planning-and-execution'
    first = engine.workflows.create_run(plan([agent()], budget_scope_id=shared))
    await engine.workflows.start_run(first['id'])
    result = await drain(engine, first)
    assert result['status'] == 'completed' and result['usage']['paused'] is True
    elapsed = result['usage']['elapsed_seconds']
    await asyncio.sleep(.02)
    assert engine.workflows.get_run(first['id'])['usage']['elapsed_seconds'] == elapsed
    second = engine.workflows.create_run(plan([agent()], budget_scope_id=shared))
    await engine.workflows.start_run(second['id'])
    engine.workflows.reconcile(first['id'])
    assert RunBudget(engine.store, shared).snapshot()['paused'] is False
    assert (await drain(engine, second))['status'] == 'completed'
    assert RunBudget(engine.store, shared).snapshot()['paused'] is True


def test_recipe_profile_policy_change_invalidates_contract(native_engine):
    engine = native_engine
    program = {'version': 1, 'steps': [{'id': 'source', 'tool': 'echo', 'inputs': {}}]}
    run = engine.workflows.create_run(plan([agent()]))
    stored = engine.workflows.nodes(run['id'])[0]
    job = engine.store.get_job(run['job_id'])
    before, owner, _ = engine.workflows.native.recipes._contract(run, stored, job, program)
    engine.profile = lambda mission: {'id': 'public', 'policy_version': 'changed'}
    after, same_owner, _ = engine.workflows.native.recipes._contract(run, stored, job, program)
    assert before != after and owner == same_owner


async def test_missing_native_usage_is_unknown_and_blocks_later_work(native_engine):
    engine = native_engine
    engine.backend.suppress_usage = True
    async def script(session, prompt):
        await session.call('echo', {'value': 1})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    result = await drain(engine, run)
    assert result['status'] == 'paused_budget'
    assert result['usage']['usage_complete'] is False and result['usage']['paused'] is True
    assert len(engine.capabilities.calls) == 1


async def test_safe_http_error_stays_in_same_native_session(native_engine):
    import httpx
    engine = native_engine
    async def broken(args, runtime): raise httpx.InvalidURL('bad redirect')
    engine.capabilities.handlers['broken_fetch'] = broken
    async def script(session, prompt):
        failed = await session.call('broken_fetch', {})
        assert failed['code'] == 'http_transport_error' and failed['recoverable']
        await session.call('echo', {'value': 1})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    assert len(engine.backend.sessions) == 1
    failure = next(row for row in engine.store.list_documents('workflow.operation', run['job_id']) if row['tool'] == 'broken_fetch')
    assert failure['status'] == 'completed' and failure['output']['error']


async def test_failed_native_attempt_escalates_only_same_family_repair(native_engine):
    engine = native_engine
    async def fail(session, prompt): raise RuntimeError('provider fixture failure')
    engine.backend.script = fail
    original = plan([agent()])
    run = engine.workflows.create_run(original)
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'needs_replan'
    parent = engine.workflows.nodes(run['id'])[0]
    assert parent['native_failures'] == 1
    repaired = plan([agent(instructions='Correct the provider invocation and preserve receipts')])
    await engine.workflows.revise_run(run['id'], repaired, approved=True)
    assert engine.workflows.nodes(run['id'])[0]['native_failures'] == 1
    changed = plan([agent(goal='A different operation')])
    await engine.workflows.revise_run(run['id'], changed, approved=True)
    assert engine.workflows.nodes(run['id'])[0].get('native_failures', 0) == 0


async def test_failed_replay_is_not_reported_as_verified_reuse(native_engine):
    engine = native_engine
    digest = 'b' * 64
    def verifier(inputs, evidence):
        actual = [r['output']['value'] for r in evidence['receipts'] if r['status'] == 'completed']
        return {'passed': inputs['value'] != 99 and actual == [inputs['value']],
                'validator_digest': digest, 'case_digest': canonical_digest(actual)}
    engine.recipe_verifiers = {'fixture': {'digest': digest, 'verify': verifier}}
    program = {'version': 1, 'steps': [{'id': 'source', 'tool': 'echo', 'inputs': {'value': {'$ref': 'inputs.value'}}}], 'output': {'done': True}}
    async def cold_script(session, prompt):
        for value in range(5):
            await session.call('recipe.execute', {'program': program, 'inputs': {'value': value}})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = cold_script
    cold = engine.workflows.create_run(plan([agent(completion={'validator': 'fixture'})]))
    await engine.workflows.start_run(cold['id'])
    assert (await drain(engine, cold))['status'] == 'completed'
    async def fallback(session, prompt):
        assert prompt['prior_execution']['reason'] == 'RecipeError'
        await session.call('echo', {'value': 100})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = fallback
    warm = engine.workflows.create_run(plan([agent(completion={'validator': 'fixture'}, replay={'program': program, 'inputs': {'value': 99}})]))
    await engine.workflows.start_run(warm['id'])
    result = await drain(engine, warm)
    assert result['status'] == 'completed' and result['execution_runtime'] == 'native'
    assert result['reuse']['verified_reuses'] == 0
    executions = engine.store.list_documents('workflow.recipe_execution', warm['job_id'])
    assert executions[0]['status'] == 'verification_failed'
    assert executions[0]['independent_verification'] == 'failed'

async def test_quota_wait_schedules_resume_preserves_receipts_and_budget(native_engine, monkeypatch):
    from datetime import timedelta
    from ore.store import utcnow
    from ore import store as store_module
    engine = native_engine
    reset = utcnow() + timedelta(seconds=120)
    engine.store.put_document('credential.quota', ['scopus', 'shared:scopus', 'search'],
        {'source': 'scopus', 'operation': 'search', 'status': 'quota_exhausted', 'resets_at': reset.timestamp()})
    async def script(session, prompt):
        if engine.backend.rounds['parent'] == 1:
            await session.call('echo', {'value': 'retained'})
            await session.call('workflow.wait_for_source', {'source': 'scopus'})
        else:
            assert prompt['continuation']['reason'] == 'source_quota_reset'
            await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()], mission={'sources': ['scopus']}))
    await engine.workflows.start_run(run['id'])
    await one(engine, run)
    current = engine.workflows.get_run(run['id'])
    assert current['status'] == 'running'
    waiting = engine.workflows.nodes(run['id'])[0]
    assert waiting['status'] == 'retry_wait' and waiting['source_waits'] == 1
    assert engine.store.claim_task('too-early', job_id=run['job_id']) is None
    receipts = engine.store.list_documents('workflow.operation', run['job_id'])
    assert len(receipts) == 1 and receipts[0]['status'] == 'completed'
    original_budget = engine.store.get_job(run['job_id'])['mission']['budget']
    monkeypatch.setattr(store_module, 'utcnow', lambda: reset + timedelta(seconds=1))
    await one(engine, run)
    assert engine.workflows.get_run(run['id'])['status'] == 'completed', [(n.get('error'),n.get('last_error')) for n in engine.workflows.nodes(run['id'])]
    assert engine.capabilities.calls == [('echo', {'value': 'retained'})]
    assert engine.store.get_job(run['job_id'])['mission']['budget'] == original_budget


async def test_source_wait_with_unknown_reset_remains_visible(native_engine):
    engine = native_engine
    async def script(session, prompt):
        await session.call('workflow.wait_for_source', {'source': 'scopus'})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()], mission={'sources': ['scopus']}))
    await engine.workflows.start_run(run['id']); await one(engine, run)
    assert engine.workflows.get_run(run['id'])['status'] == 'awaiting_source'
    assert engine.store.claim_task('retry', job_id=run['job_id']) is None


async def test_source_wait_cannot_authorize_an_unrequested_source(native_engine):
    engine = native_engine
    async def script(session, prompt):
        result = await session.call('workflow.wait_for_source', {'source': 'scopus'})
        assert result['error'] and result['code'] == 'AccessDenied'
        await session.call('echo', {'value': 'done'})
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()], mission={'sources': ['pubmed']}))
    await engine.workflows.start_run(run['id']); await one(engine, run)
    assert engine.workflows.get_run(run['id'])['status'] == 'completed', [(n.get('error'),n.get('last_error')) for n in engine.workflows.nodes(run['id'])]


async def test_user_pause_prevents_scheduled_quota_resume(native_engine, monkeypatch):
    from datetime import timedelta
    from ore.store import utcnow
    from ore import store as store_module
    engine = native_engine
    reset = utcnow() + timedelta(seconds=120)
    engine.store.put_document('credential.quota', ['scopus', 'shared:scopus', 'search'],
        {'source': 'scopus', 'operation': 'search', 'status': 'quota_exhausted', 'resets_at': reset.timestamp()})
    async def script(session, prompt):
        await session.call('workflow.wait_for_source', {'source': 'scopus'})
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()], mission={'sources': ['scopus']}))
    await engine.workflows.start_run(run['id']); await one(engine, run)
    await engine.workflows.interrupt(run['id'])
    monkeypatch.setattr(store_module, 'utcnow', lambda: reset + timedelta(seconds=1))
    assert engine.store.claim_task('after-reset', job_id=run['job_id']) is None


async def test_quiescent_failed_native_work_does_not_spend_idle_time(native_engine):
    engine = native_engine
    async def script(session, prompt):
        await session.call('echo', {'value': 'retained'})
        raise ValueError('fixture local execution failure')
    engine.backend.script = script
    run = engine.workflows.create_run(plan([agent()]))
    await engine.workflows.start_run(run['id']); await one(engine, run)
    result = engine.workflows.get_run(run['id'])
    assert result['status'] == 'needs_replan' and result['usage']['paused'] is True
    elapsed = result['usage']['elapsed_seconds']
    await asyncio.sleep(.02)
    assert engine.workflows.get_run(run['id'])['usage']['elapsed_seconds'] == elapsed
