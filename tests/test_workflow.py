"""Workflow correctness uses real SQLite transactions and scripted tools/models."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from ore.models import canonical_digest
from ore.store import LeaseLost, Store, tasks, utcnow
from ore.workflow import WorkflowError, WorkflowManager, evaluate, normalize_nodes


class Registry:
    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.version = 'fixture-v1'
        self.replay_safe = {}

    def catalog(self):
        return [{'name': name, 'version': self.version, 'digest': canonical_digest([name, self.version]), 'read_only': True, 'replay_safe': self.replay_safe.get(name, False)}
                for name in {'echo', 'sum', 'block', 'append'} | set(self.handlers)]

    async def execute(self, name, args, runtime):
        self.calls.append((name, args))
        if name in self.handlers: return await self.handlers[name](args, runtime)
        if name == 'sum': return {'value': sum(args['values'])}
        if name == 'append':
            return runtime.engine.store.record_observation(runtime.job_id, {'kind': 'fixture', **args}, lease=runtime.lease)
        return args


@pytest.fixture
def engine(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'workflow.sqlite')); store.initialize()
    result = SimpleNamespace(store=store, capabilities=Registry(), start=AsyncMock(), running={},
        execution=SimpleNamespace(enabled=False), settings=SimpleNamespace(state_dir=tmp_path),
        profile=lambda mission: {'id': 'public'}, event=lambda job, name, args: store.append_event(job, name, args))
    result.workflows = WorkflowManager(result)
    yield result
    store.close()


def plan(nodes, **extra):
    return {'goal': 'Fixture data processing', 'artifact_roles': [], 'workflow': {'schema_version': 'ore.workflow/v1', 'nodes': nodes}, **extra}


def node(ident, inputs=None, **extra):
    return {'id': ident, 'kind': 'tool', 'tool': 'echo', 'inputs': inputs or {}, **extra}


async def one(engine, run):
    task = engine.store.claim_task('fixture-worker', job_id=run['job_id'])
    assert task, engine.workflows.nodes(run['id'])
    await engine.workflows.execute_task(task, 'fixture-worker')
    return task


async def drain(engine, run, limit=100):
    for _ in range(limit):
        task = engine.store.claim_task('fixture-worker', job_id=run['job_id'])
        if not task: break
        await engine.workflows.execute_task(task, 'fixture-worker')
    else: pytest.fail('Workflow failed to settle within the fixture task budget')
    return engine.workflows.get_run(run['id'])


async def test_explicit_approval_and_dag_dependency_outputs(engine):
    manager = engine.workflows
    run = manager.create_run(plan([node('a', {'value': 3}), node('b', {'copied': {'$ref': 'nodes.a.output.value'}}, depends_on=['a'])]))
    assert manager.get_run(run['id'])['status'] == 'draft'
    assert engine.store.tasks(run['job_id']) == []
    assert engine.store.claim_task('unauthorized') is None
    await manager.start_run(run['id'])
    assert [t['input']['node_id'] for t in engine.store.tasks(run['job_id'])] == ['a']
    await one(engine, run)
    assert [t['input']['node_id'] for t in engine.store.tasks(run['job_id'])] == ['a', 'b']
    finished = await drain(engine, run)
    assert finished['status'] == 'completed'
    assert finished['output']['b'] == {'copied': 3}
    assert manager.audit(run['id'])['status'] == 'complete_within_scope'
    assert engine.capabilities.calls == [('echo', {'value': 3}), ('echo', {'copied': 3})]


async def test_recipe_has_zero_model_calls_and_persistent_step_outputs(engine):
    engine.backend = SimpleNamespace(run=AsyncMock(side_effect=AssertionError('No model is permitted for a recipe')))
    run = engine.workflows.create_run(plan([{'id': 'recipe', 'kind': 'recipe', 'steps': [
        {'id': 'first', 'tool': 'echo', 'inputs': {'values': [2, 4]}},
        {'id': 'second', 'tool': 'sum', 'inputs': {'values': {'$ref': 'steps.first.output.values'}}}],
        'checks': [{'op': 'eq', 'left': {'$ref': 'output.value'}, 'right': 6}]}]))
    await engine.workflows.start_run(run['id']); finished = await drain(engine, run)
    assert finished['status'] == 'completed'
    assert engine.backend.run.await_count == 0
    receipts = engine.store.list_documents('workflow.operation', run['job_id'])
    assert len(receipts) == 2 and all(row['status'] == 'completed' for row in receipts)


async def test_lazy_foreach_and_condition_only_materialize_selected_branch(engine):
    run = engine.workflows.create_run(plan([
        {'id': 'items', 'kind': 'foreach', 'items': [1, 2, 3], 'batch_size': 1,
         'body': [node('copy', {'item': {'$ref': 'item'}})]},
        {'id': 'choose', 'kind': 'condition', 'condition': {'op': 'eq', 'left': 2, 'right': 2},
         'then': [node('yes', {'selected': True})], 'else': [node('no', {'selected': False})], 'depends_on': ['items']}]))
    await engine.workflows.start_run(run['id'])
    assert len(engine.workflows.nodes(run['id'])) == 2
    await one(engine, run)
    assert len(engine.workflows.nodes(run['id'])) == 3  # Only first iteration exists.
    assert engine.store.get_task(engine.store.tasks(run['job_id'])[0]['id'])['state'] == 'succeeded'
    assert (await drain(engine, run))['status'] == 'completed'
    ids = {n['id'] for n in engine.workflows.nodes(run['id'])}
    assert {'items/0/copy', 'items/1/copy', 'items/2/copy', 'choose/yes'} <= ids
    assert 'choose/no' not in ids
    assert [args for name, args in engine.capabilities.calls] == [{'item': 1}, {'item': 2}, {'item': 3}, {'selected': True}]


async def test_restart_does_not_repeat_completed_recipe_step(engine):
    run = engine.workflows.create_run(plan([{'id': 'recipe', 'kind': 'recipe', 'steps': [
        {'id': 'one', 'tool': 'echo', 'inputs': {'value': 1}}, {'id': 'two', 'tool': 'echo', 'inputs': {'value': 2}}]}]))
    await engine.workflows.start_run(run['id'])
    task = engine.store.claim_task('first', job_id=run['job_id'])
    from ore.tools import ToolRuntime
    runtime = ToolRuntime(engine, run['job_id'], task)
    await engine.workflows._tool(task, runtime, 'echo', {'value': 1}, 'one')
    # The process disappears after a durable step receipt but before node finish.
    with engine.store._tx() as conn:
        conn.execute(update(tasks).where(tasks.c.id == task['id']).values(lease_expires_at=utcnow()))
    engine.workflows = WorkflowManager(engine)
    assert (await drain(engine, run))['status'] == 'completed'
    assert engine.capabilities.calls == [('echo', {'value': 1}), ('echo', {'value': 2})]


async def test_unknown_operation_outcome_is_not_replayed_after_restart(engine):
    run = engine.workflows.create_run(plan([node('a')]))
    await engine.workflows.start_run(run['id']); task = engine.store.claim_task('old', job_id=run['job_id'])
    item = engine.workflows.nodes(run['id'])[0]
    key = [run['id'], 'a', item.get('execution_digest', item['spec_digest']), 'tool']
    engine.store.put_document('workflow.operation', key, {'id': canonical_digest(key), 'status': 'started',
        'input_digest': canonical_digest({'name': 'echo', 'arguments': {}})}, job_id=run['job_id'])
    with engine.store._tx() as conn:
        conn.execute(update(tasks).where(tasks.c.id == task['id']).values(lease_expires_at=utcnow()))
    engine.workflows = WorkflowManager(engine)
    finished = await drain(engine, run)
    assert finished['status'] == 'needs_reconciliation'
    assert engine.capabilities.calls == []


async def test_partial_interrupt_fences_affected_writes_and_preserves_other_branch(engine):
    manager = engine.workflows
    run = manager.create_run(plan([node('a'), node('after', depends_on=['a']), node('independent', {'ok': True})]))
    await manager.start_run(run['id']); task = engine.store.claim_task('unreachable', job_id=run['job_id'])
    assert task['input']['node_id'] == 'a'
    before = engine.store.get_job(run['job_id'])
    stopped = await manager.interrupt(run['id'], ['a'])
    assert stopped['status'] == 'running' and not stopped['interrupt_confirmed']
    assert engine.store.get_job(run['job_id'])['revision'] == before['revision']
    lease = {'task_id': task['id'], **{k: task[k] for k in ('worker_id', 'fence', 'revision')}}
    with pytest.raises(LeaseLost): engine.store.record_observation(run['job_id'], {'kind': 'late'}, lease=lease)
    next_task = await one(engine, run)
    assert next_task['input']['node_id'] == 'independent'
    rows = {n['id']: n for n in manager.nodes(run['id'])}
    assert rows['a']['status'] == rows['after']['status'] == 'paused'
    assert rows['independent']['status'] == 'succeeded'
    with pytest.raises(WorkflowError, match='unconfirmed'): await manager.resume(run['id'])
    manager.acknowledge_interrupt(run['id'], task['id'], task['fence'], 'unreachable')
    await manager.resume(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    assert len([x for x in engine.capabilities.calls if x[1].get('ok')]) == 1


async def test_stop_blocks_late_commit_even_when_tool_swallows_cancellation(engine):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(args, runtime):
        entered.set()
        try: await release.wait()
        except asyncio.CancelledError: await release.wait()
        return runtime.engine.store.record_observation(runtime.job_id, {'kind': 'late'}, lease=runtime.lease)
    engine.capabilities.handlers['block'] = blocked
    run = engine.workflows.create_run(plan([node('a', tool='block')]))
    await engine.workflows.start_run(run['id']); task = engine.store.claim_task('worker', job_id=run['job_id'])
    future = asyncio.create_task(engine.workflows.execute_task(task, 'worker'))
    await entered.wait()
    result = await engine.workflows.interrupt(run['id'])
    assert result['status'] == 'interrupting'
    assert engine.store.claim_task('new', job_id=run['job_id']) is None
    release.set(); await future
    assert engine.workflows.get_run(run['id'])['status'] == 'paused'
    assert not engine.store.observations(run['job_id'])
    assert not engine.store.artifacts(run['job_id'])


async def test_stop_lazy_parent_prevents_future_expansion(engine):
    run = engine.workflows.create_run(plan([{'id': 'loop', 'kind': 'foreach', 'items': [1, 2], 'batch_size': 1,
        'body': [node('child', {'value': {'$ref': 'item'}})]}]))
    await engine.workflows.start_run(run['id']); await one(engine, run)
    await engine.workflows.interrupt(run['id'], ['loop'])
    assert len(engine.workflows.nodes(run['id'])) == 2
    assert engine.store.claim_task('new', job_id=run['job_id']) is None
    await engine.workflows.resume(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
    assert len(engine.workflows.nodes(run['id'])) == 3


async def test_remote_interrupt_requires_authenticated_cancel_receipt_not_lease_expiry(engine):
    run = engine.workflows.create_run(plan([node('a')]))
    await engine.workflows.start_run(run['id']); task = engine.store.claim_task('worker', job_id=run['job_id'])
    engine.store.put_document('execution.assignment', 'assignment', {'id': 'assignment', 'task': task}, job_id=run['job_id'])
    engine.store.put_document('execution.command', 'cmd', {'id': 'cmd', 'assignment_id': 'assignment', 'task_fence': task['fence'], 'status': 'running'}, job_id=run['job_id'])
    await engine.workflows.interrupt(run['id'])
    engine.workflows.acknowledge_interrupt(run['id'], task['id'], task['fence'], task['worker_id'])
    assert not engine.workflows.get_run(run['id'])['interrupt_confirmed']
    command = engine.store.get_document('execution.command', 'cmd')
    engine.store.put_document('execution.command', 'cmd', {**command, 'status': 'cancelled'}, job_id=run['job_id'])
    assert engine.workflows.refresh_interruptions(run['id'])['status'] == 'interrupting'
    engine.store.put_document('execution.command', 'cmd', {**command, 'status': 'cancelled', 'cancel_acknowledged_at': 'observed'}, job_id=run['job_id'])
    assert engine.workflows.reconcile(run['job_id'])['status'] == 'paused'


async def test_failed_check_blocks_downstream_and_no_false_success(engine):
    run = engine.workflows.create_run(plan([node('a', {'value': 1}, checks=[{'op': 'eq', 'left': {'$ref': 'output.value'}, 'right': 2}]), node('after', depends_on=['a'])]))
    await engine.workflows.start_run(run['id']); result = await drain(engine, run)
    assert result['status'] == 'needs_replan'
    assert engine.workflows.audit(run['id'])['status'] == 'incomplete'
    assert len(engine.capabilities.calls) == 1
    assert len(engine.store.tasks(run['job_id'])) == 1


async def test_capability_version_drift_and_forbidden_tools_fail_before_execution(engine):
    with pytest.raises(WorkflowError, match='capability envelope'):
        engine.workflows.create_run(plan([node('a')], constraints={'allowed_tools': ['sum']}))
    assert engine.capabilities.calls == []
    run = engine.workflows.create_run(plan([node('a')]))
    engine.capabilities.version = 'fixture-v2'
    await engine.workflows.start_run(run['id']); result = await drain(engine, run)
    assert result['status'] == 'needs_replan' and engine.capabilities.calls == []


async def test_revision_recomputes_affected_descendants_only(engine):
    original = plan([node('a', {'value': 1}), node('after', {'copied': {'$ref': 'nodes.a.output.value'}}, depends_on=['a']), node('independent', {'kept': True})])
    run = engine.workflows.create_run(original); await engine.workflows.start_run(run['id']); await drain(engine, run)
    changed = json.loads(json.dumps(original)); changed['workflow']['nodes'][0]['inputs']['value'] = 2
    await engine.workflows.revise_run(run['id'], changed, approved=True)
    result = await drain(engine, run)
    assert result['status'] == 'completed'
    assert result['output']['after'] == {'copied': 2}
    assert len([args for _, args in engine.capabilities.calls if args.get('kept')]) == 1
    assert engine.store.get_job(run['job_id'])['revision'] == 1


async def test_carry_forward_binds_verified_blob_with_new_ids_and_original_provenance(engine, tmp_path):
    path = tmp_path / 'artifact.txt'; path.write_text('verified content')
    import hashlib
    async def artifact(args, runtime):
        resource = runtime.engine.store.upsert_resource(runtime.job_id, {'id': 'old-resource', 'title': 'Fixture'}, lease=runtime.lease)
        row = runtime.engine.store.add_artifact(runtime.job_id, {'id': 'old-artifact', 'resource_id': resource['id'], 'path': str(path),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size, 'status': 'verified', 'integrity': 'verified', 'role': 'attachment'}, lease=runtime.lease)
        return {'artifact_id': row['id']}
    engine.capabilities.handlers['artifact'] = artifact
    original = plan([node('a', tool='artifact')])
    old = engine.workflows.create_run(original); await engine.workflows.start_run(old['id']); await drain(engine, old)
    new = engine.workflows.create_run({**original, 'previous_run_id': old['id']})
    await engine.workflows.start_run(new['id'])
    assert engine.workflows.get_run(new['id'])['status'] == 'completed'
    assert len(engine.capabilities.calls) == 1
    rows = engine.store.artifacts(new['job_id']); assert len(rows) == 1
    assert rows[0]['id'] != 'old-artifact' and rows[0]['path'] == str(path)
    assert rows[0]['imported_from']['artifact_id'] == 'old-artifact'
    assert rows[0]['resource_id'] != 'old-resource'
    assert engine.workflows.get_run(new['id'])['output']['a']['artifact_id'] == rows[0]['id']


async def test_expansion_limit_preserves_completed_evidence(engine):
    run = engine.workflows.create_run(plan([node('evidence', {'kept': True}), {'id': 'loop', 'kind': 'foreach', 'items': [1, 2, 3],
        'body': [node('copy')], 'depends_on': ['evidence']}], budget={'max_tasks': 2}))
    await engine.workflows.start_run(run['id']); result = await drain(engine, run)
    assert result['status'] == 'paused_budget'
    assert next(n for n in engine.workflows.nodes(run['id']) if n['id'] == 'evidence')['output'] == {'kept': True}


@pytest.mark.parametrize('values', [[node('a', depends_on=['missing'])], [node('a', depends_on=['b']), node('b', depends_on=['a'])], [node('a'), node('a')]])
def test_invalid_graphs_are_rejected_before_creating_jobs(engine, values):
    with pytest.raises(WorkflowError): engine.workflows.create_run(plan(values))
    assert engine.store.list_jobs() == []


def test_conditions_do_not_execute_python_and_schema_refs_cannot_fetch_network(engine):
    with pytest.raises(WorkflowError): evaluate({'op': '__import__', 'value': 'os'}, {})
    with pytest.raises(WorkflowError):
        engine.workflows._check([{'type': 'schema', 'schema': {'$ref': 'https://example.org/schema'}}], {'output': {}})


async def test_agent_control_expands_to_llm_free_children_and_requires_checks(engine):
    class Backend:
        def __init__(self): self.calls = 0
        async def thread(self, *a, **kw): return 'fixture-thread'
        async def run(self, *a, **kw):
            self.calls += 1
            return {'text': json.dumps({'tool': 'workflow.expand', 'arguments': json.dumps({'nodes': [node('one', {'n': 1}), node('two', {'n': 2})]}), 'reason': 'Bounded two-item fixture'}), 'turn': {'status': 'completed'}}
        async def interrupt(self, *a): pass
    engine.backend = Backend(); engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'fixture', 'effort': 'high', 'mode': 'auto'}
    run = engine.workflows.create_run(plan([{'id': 'agent', 'kind': 'agent', 'checks': [{'op': 'eq', 'left': {'$ref': 'output.children.agent/one.n'}, 'right': 1}]}]))
    await engine.workflows.start_run(run['id']); result = await drain(engine, run)
    assert result['status'] == 'completed'
    assert engine.backend.calls == 1
    assert len(engine.capabilities.calls) == 2


async def test_agent_summary_without_checks_does_not_complete(engine):
    backend = SimpleNamespace(thread=AsyncMock(return_value='thread'), interrupt=AsyncMock(),
        run=AsyncMock(return_value={'text': json.dumps({'tool': 'finish', 'arguments': json.dumps({'summary': 'Done'}), 'reason': 'claim'}), 'turn': {'status': 'completed'}}))
    engine.backend = backend; engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'fixture', 'effort': 'high', 'mode': 'auto'}
    run = engine.workflows.create_run(plan([{'id': 'agent', 'kind': 'agent'}]))
    await engine.workflows.start_run(run['id']); result = await drain(engine, run)
    assert result['status'] == 'needs_replan'
    assert backend.run.await_count == 3


def test_quality_first_downgrade_requires_matching_task_and_check_evidence(engine):
    calls = []
    engine.routing_for = lambda job, kind, failures, catalog: calls.append(kind) or {'model': 'highest', 'effort': 'high', 'mode': 'auto'}
    spec = {'id': 'a', 'kind': 'agent', 'purpose': 'extract', 'checks': [{'op': 'truthy', 'value': {'$ref': 'output'}}]}
    run = engine.workflows.create_run(plan([spec])); item = engine.workflows.nodes(run['id'])[0]
    job = engine.store.get_job(run['job_id'])
    route = engine.workflows._routing(job, item, [], 0)
    assert calls[-1] == 'plan' and not route['task_scoped_validation']
    engine.profile = lambda mission: {'workflow_routing_validation': {item['spec_digest']: {
        'task_digest': item['spec_digest'], 'checks_digest': canonical_digest(item['spec']['checks']), 'approved': True}}}
    route = engine.workflows._routing(job, item, [], 0)
    assert calls[-1] == 'extract' and route['task_scoped_validation']


def test_compile_plan_rejects_unexecutable_model_proposals_without_side_effects(engine):
    for invalid in [plan([node('a', checks=['download everything'])]), plan([node('a', tool='finish')]),
                    plan([node('a', checks=[{'op': 'python', 'value': '1'}])]),
                    plan([node('a', {'ref': {'$ref': 'nodes.not_here.output'}})]),
                    plan([node('a'), node('b', {'ref': {'$ref': 'nodes.a.output'}})])]:
        with pytest.raises(WorkflowError): engine.workflows.validate_plan(invalid)
    assert engine.store.list_jobs() == []
    assert engine.capabilities.calls == []
    valid = engine.workflows.validate_plan(plan([node('a', {'value': 1}, checks=[{'op': 'eq', 'left': {'$ref': 'output.value'}, 'right': 1}])]))
    assert valid['workflow']['nodes'][0]['kind'] == 'tool'


async def test_automatic_family_calibration_promotes_after_five_pairs_and_reverts_on_failure(engine, monkeypatch):
    monkeypatch.setattr('ore.evaluation.runtime_fingerprint', lambda: {'digest': 'frozen-fixture-runtime'})
    class Backend:
        def __init__(self): self.calls = []; self.number = 0; self.fail_low = False
        async def thread(self, *args, **kwargs): self.number += 1; return str(self.number)
        async def interrupt(self, *args): pass
        async def run(self, tid, prompt, **kwargs):
            expected = json.loads(prompt)['context']['inputs']['expected']
            self.calls.append({'model': kwargs['model'], 'effort': kwargs['effort'], 'expected': expected})
            output = expected + 1 if self.fail_low and kwargs['model'] == 'gpt-5.6-terra' else expected
            return {'text': json.dumps({'tool': 'workflow.finish', 'arguments': json.dumps({'output': output}), 'reason': 'Exact fixture response'}), 'turn': {'status': 'completed'}}
    engine.backend = Backend()
    catalog = [{'model': 'gpt-6-astra', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'medium'}]},
               {'model': 'gpt-5.6-terra', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}]
    engine.models = AsyncMock(return_value=catalog)
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'gpt-6-astra', 'effort': 'high', 'mode': 'auto'}
    def collection(items):
        return plan([{'id': 'loop', 'kind': 'foreach', 'items': items, 'batch_size': 1, 'body': [{
            'id': 'copy', 'kind': 'agent', 'inputs': {'expected': {'$ref': 'item'}},
            'checks': [{'op': 'eq', 'left': {'$ref': 'output'}, 'right': {'$ref': 'inputs.expected'}}]}]}],
            budget={'max_turns': 40, 'adaptation_max_shadow_turns': 10})
    run = engine.workflows.create_run(collection([1, 2, 3, 4, 5, 6]))
    await engine.workflows.start_run(run['id']); assert (await drain(engine, run))['status'] == 'completed'
    for value in range(1, 6):
        assert [x['model'] for x in engine.backend.calls if x['expected'] == value] == ['gpt-6-astra', 'gpt-5.6-terra']
    assert [x['model'] for x in engine.backend.calls if x['expected'] == 6] == ['gpt-5.6-terra']
    evidence = engine.store.list_documents('workflow.calibration')
    assert len(evidence) == 1 and evidence[0]['verified_pairs'] == 5 and evidence[0]['valid']
    assert engine.capabilities.calls == []  # Shadows never execute a tool.
    engine.backend.fail_low = True
    failing = engine.workflows.create_run(collection([7])); await engine.workflows.start_run(failing['id'])
    assert (await drain(engine, failing))['status'] == 'needs_replan'
    assert not engine.store.list_documents('workflow.calibration')[0]['valid']
    engine.backend.fail_low = False
    fresh = engine.workflows.create_run(collection([8])); await engine.workflows.start_run(fresh['id']); await drain(engine, fresh)
    assert [x['model'] for x in engine.backend.calls if x['expected'] == 8][0] == 'gpt-6-astra'


def test_adaptation_uses_supported_effort_downshift_and_rejects_weak_checks(engine):
    from ore.workflow_adaptation import strong_checks
    assert not strong_checks([{'op': 'truthy', 'value': {'$ref': 'output'}}])
    assert not strong_checks([{'type': 'schema', 'schema': {'type': 'object'}}])
    catalog = [{'model': 'private-fixture', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'medium'}]}]
    assert engine.workflows.adaptation.candidates(catalog, {'model': 'private-fixture', 'effort': 'high'}) == [{'model': 'private-fixture', 'effort': 'medium'}]


@pytest.mark.parametrize('replay_safe', [True, False])
async def test_confirmed_interrupted_replay_safe_tool_resumes_but_mutating_tool_holds(engine, replay_safe):
    entered = asyncio.Event(); calls = 0
    async def block(args, runtime):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set(); await asyncio.Event().wait()
        return {'done': True}
    engine.capabilities.handlers['block'] = block
    engine.capabilities.replay_safe['block'] = replay_safe
    run = engine.workflows.create_run(plan([node('a', tool='block')]))
    await engine.workflows.start_run(run['id']); task = engine.store.claim_task('local', job_id=run['job_id'])
    future = asyncio.create_task(engine.workflows.execute_task(task, 'local')); await entered.wait()
    assert (await engine.workflows.interrupt(run['id']))['status'] == 'paused'
    await asyncio.gather(future, return_exceptions=True)
    await engine.workflows.resume(run['id']); result = await drain(engine, run)
    assert result['status'] == ('completed' if replay_safe else 'needs_reconciliation')
    assert calls == (2 if replay_safe else 1)
    receipt = engine.store.list_documents('workflow.operation', run['job_id'])[0]
    if replay_safe: assert len(receipt['prior_attempts']) == 1


async def test_shutdown_durably_pauses_and_requires_explicit_resume(engine):
    run = engine.workflows.create_run(plan([node('a')]))
    await engine.workflows.start_run(run['id']); await engine.workflows.close()
    engine.workflows = WorkflowManager(engine)
    assert engine.workflows.get_run(run['id'])['status'] == 'paused'
    assert engine.store.claim_task('new-process', job_id=run['job_id']) is None
    await engine.workflows.resume(run['id']); assert (await drain(engine, run))['status'] == 'completed'


async def test_changed_plan_parameter_does_not_reuse_old_output(engine):
    spec = plan([node('a', {'value': {'$ref': 'plan.parameter'}})], parameter=1)
    old = engine.workflows.create_run(spec); await engine.workflows.start_run(old['id']); await drain(engine, old)
    fresh = engine.workflows.create_run({**spec, 'parameter': 2, 'previous_run_id': old['id']})
    await engine.workflows.start_run(fresh['id']); result = await drain(engine, fresh)
    assert result['output']['a']['value'] == 2 and len(engine.capabilities.calls) == 2


async def test_systematic_workflow_does_not_report_complete_when_domain_inventory_missing(engine, monkeypatch):
    monkeypatch.setattr('ore.coverage.audit_coverage', lambda *a, **kw: {'status': 'incomplete', 'gaps': [{'kind': 'inventory_unsealed'}]})
    run = engine.workflows.create_run(plan([node('a')], completeness='systematic'))
    await engine.workflows.start_run(run['id']); result = await drain(engine, run)
    assert result['status'] == 'finished_incomplete'
    assert engine.workflows.audit(run['id'])['status'] == 'incomplete'


def test_custom_calibration_order_uses_only_live_supported_pairs(engine):
    catalog = [{'model': 'private-high', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]},
               {'model': 'private-small', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}]
    choices = engine.workflows.adaptation.candidates(catalog, {'model': 'private-high', 'effort': 'high'},
        {'candidates': [{'model': 'missing', 'effort': 'low'}, {'model': 'private-small', 'effort': 'ultra'}, {'model': 'private-small', 'effort': 'low'}]})
    assert choices == [{'model': 'private-small', 'effort': 'low'}]


def test_oversized_initial_graph_fails_before_job_creation(engine):
    with pytest.raises(WorkflowError, match='task budget'):
        engine.workflows.create_run(plan([node('a'), node('b')], budget={'max_tasks': 1}))
    assert not engine.store.list_jobs()


async def test_provider_interruption_ack_is_separate_from_local_coroutine_exit(engine):
    entered = asyncio.Event()
    class Backend:
        acknowledged = False
        async def thread(self, *args, **kwargs): return 'provider-thread'
        async def run(self, *args, **kwargs): entered.set(); await asyncio.Event().wait()
        async def interrupt(self, tid): return {'thread_id': tid, 'requested': True, 'acknowledged': self.acknowledged}
    engine.backend = Backend(); engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'fixture', 'effort': 'high', 'mode': 'fixed'}
    run = engine.workflows.create_run(plan([{'id': 'a', 'kind': 'agent'}]))
    await engine.workflows.start_run(run['id']); task = engine.store.claim_task('local', job_id=run['job_id'])
    future = asyncio.create_task(engine.workflows.execute_task(task, 'local')); await entered.wait()
    stopped = await engine.workflows.interrupt(run['id']); await asyncio.gather(future, return_exceptions=True)
    assert stopped['status'] == 'interrupting'
    assert stopped['interruptions'][0]['worker_ack'] is True
    assert stopped['model_interrupt_status'][0]['acknowledged'] is False
    with pytest.raises(WorkflowError, match='unconfirmed'): await engine.workflows.resume(run['id'])
    engine.backend.acknowledged = True
    await engine.workflows.retry_model_interrupts(run['id'])
    assert engine.workflows.get_run(run['id'])['status'] == 'paused'


async def test_recipe_plan_reference_change_recomputes_within_same_run(engine):
    original = plan([{'id': 'a', 'kind': 'recipe', 'steps': [{'id': 'copy', 'tool': 'echo', 'inputs': {'value': {'$ref': 'plan.parameter'}}}]}], parameter=1)
    run = engine.workflows.create_run(original); await engine.workflows.start_run(run['id']); await drain(engine, run)
    await engine.workflows.revise_run(run['id'], {**original, 'parameter': 2}, approved=True)
    result = await drain(engine, run)
    assert result['status'] == 'completed' and result['output']['a']['value'] == 2


async def test_unchanged_completed_lazy_subtree_is_carried_without_reexecution(engine):
    original = plan([{'id': 'loop', 'kind': 'foreach', 'items': {'$ref': 'plan.items'}, 'body': [node('copy', {'value': {'$ref': 'item'}})]}], items=[1, 2])
    old = engine.workflows.create_run(original); await engine.workflows.start_run(old['id']); await drain(engine, old)
    fresh = engine.workflows.create_run({**original, 'previous_run_id': old['id']})
    await engine.workflows.start_run(fresh['id'])
    assert engine.workflows.get_run(fresh['id'])['status'] == 'completed'
    assert len(engine.capabilities.calls) == 2
    assert len(engine.workflows.nodes(fresh['id'])) == 3
    changed = engine.workflows.create_run({**original, 'items': [3, 4], 'previous_run_id': old['id']})
    await engine.workflows.start_run(changed['id']); assert (await drain(engine, changed))['status'] == 'completed'
    assert [args['value'] for _, args in engine.capabilities.calls] == [1, 2, 3, 4]


async def test_unconfirmed_sandbox_cleanup_is_durable_and_resume_retries_cleanup(engine, monkeypatch):
    from ore.sandbox import SandboxTerminationUnconfirmed
    attempts = 0
    async def code(args, runtime):
        nonlocal attempts
        attempts += 1
        if attempts == 1: raise SandboxTerminationUnconfirmed('ore-code-fixture')
        return {'done': True}
    engine.capabilities.handlers['code'] = code
    engine.capabilities.replay_safe['code'] = True
    run = engine.workflows.create_run(plan([node('a', tool='code')]))
    await engine.workflows.start_run(run['id']); task = engine.store.claim_task('local', job_id=run['job_id'])
    with pytest.raises(asyncio.CancelledError): await engine.workflows.execute_task(task, 'local')
    stopped = engine.workflows.get_run(run['id'])
    assert stopped['status'] == 'needs_reconciliation'
    assert stopped['resource_interrupt_status'][0]['acknowledged'] is False
    monkeypatch.setattr('ore.sandbox.ensure_container_stopped', AsyncMock(return_value=False))
    with pytest.raises(WorkflowError, match='unconfirmed'): await engine.workflows.resume(run['id'])
    monkeypatch.setattr('ore.sandbox.ensure_container_stopped', AsyncMock(return_value=True))
    await engine.workflows.resume(run['id']); assert (await drain(engine, run))['status'] == 'completed'
    assert attempts == 2


async def test_foreach_refills_free_iteration_while_earlier_child_is_still_running(engine):
    run = engine.workflows.create_run(plan([{'id': 'loop', 'kind': 'foreach', 'items': [0, 1, 2, 3],
        'batch_size': 2, 'body': [node('copy', {'item': {'$ref': 'item'}})]}]))
    await engine.workflows.start_run(run['id']); await one(engine, run)
    slow = engine.store.claim_task('slow', job_id=run['job_id'])
    assert slow['input']['node_id'] == 'loop/0/copy'
    await one(engine, run)
    ids = {n['id'] for n in engine.workflows.nodes(run['id'])}
    assert 'loop/2/copy' in ids and 'loop/3/copy' not in ids
    assert engine.store.get_task(slow['id'])['state'] == 'running'
    await engine.workflows.execute_task(slow, 'slow')
    assert (await drain(engine, run))['status'] == 'completed'
