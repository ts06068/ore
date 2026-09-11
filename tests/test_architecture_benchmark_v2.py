"""Finite harness/fairness checks; no provider calls or official holdout seeds."""
from copy import deepcopy
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_original_path = list(sys.path)
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.benchmark_architecture_v2 import CommonPrimitives, NativeActor, Usage, one, prepare, shared_instructions
    from scripts.benchmark_evaluation_v2 import ARMS, NEW_BUDGET, OLD_BUDGET, evaluate_report, preregistration, schedule
    from scripts.benchmark_fixtures_v2 import EvaluationFixture, SharedWorkspace
finally:
    sys.path[:] = _original_path

TEST_SEED = b'unit-test-only-not-an-official-holdout-seed'


def frozen_manifest():
    value = preregistration(hashlib.sha256(TEST_SEED).hexdigest())
    value.update(preregistration_digest='unit-test-registration', frozen_fingerprint={'runtime': 'test'})
    return value


def perfect_rows():
    manifest = frozen_manifest()
    rows = []
    for item in manifest['schedule']:
        ratio = .75 if item['arm'] == 'ore' and item['lifecycle_id'] else 1
        row = {**deepcopy(item), 'passed': True, 'first_attempt_passed': True, 'status': 'completed',
               'preregistration_digest': manifest['preregistration_digest'],
               'fingerprint_start': manifest['frozen_fingerprint'], 'fingerprint_end': manifest['frozen_fingerprint'],
               'usage': {'usage_complete': True, 'tokens': {'totalTokens': int(10000 * ratio), 'cachedInputTokens': 6000}},
               'work_seconds': 100 * ratio, 'tool_contract_digest': 'shared-tools', 'semantic_request_digest': item['pair_id'], 'grade': {'source_contract_digest': item['pair_id']}}
        rows.append(row)
    return rows, manifest


def test_schedule_is_fixed_72_and_each_lifecycle_includes_cold_cost():
    rows = schedule()
    assert len(rows) == len({row['case_id'] for row in rows}) == 72
    assert sum(row['split'] == 'regression' for row in rows) == 36
    assert sum(row['split'] == 'holdout' and row['phase'] == 'cold' for row in rows) == 24
    assert sum(row['phase'] != 'cold' for row in rows) == 12
    assert all(row['budget']['max_agent_workers'] == 5 for row in rows)
    for family in ('main_and_supplements', 'repeated_extraction', 'restart_failure'):
        for arm in ARMS:
            phases = {r['phase'] for r in rows if r['family'] == family and r['arm'] == arm and r['lifecycle_id']}
            assert phases == {'cold', 'unchanged', 'changed'}
    assert {r['source_items'] for r in rows if r['split'] == 'holdout'} >= {100, 1000}


def test_gate_requires_both_improvements_and_no_family_regression():
    rows, manifest = perfect_rows()
    assert evaluate_report(rows, manifest)['release_gate_passed']
    for row in rows:
        if row['arm'] == 'ore' and row['lifecycle_id']:
            row['usage']['tokens']['totalTokens'] = 9000
    report = evaluate_report(rows, manifest)
    assert not report['release_gate_passed']
    assert 'lifecycle_token_improvement_below_20_percent_or_unknown' in report['gate_failures']
    rows, manifest = perfect_rows()
    for row in rows:
        if row['arm'] == 'ore' and row['lifecycle_id'] and row['family'] == 'main_and_supplements':
            row['work_seconds'] = 101
    report = evaluate_report(rows, manifest)
    assert 'lifecycle_family_wall_regression_or_unknown' in report['gate_failures']


def test_gate_rejects_filtered_failures_missing_usage_and_changed_contract():
    rows, manifest = perfect_rows()
    rows[1]['passed'] = False
    assert not evaluate_report(rows, manifest)['release_gate_passed']
    assert not evaluate_report(rows[:-1], manifest)['release_gate_passed']
    rows, manifest = perfect_rows()
    rows[0]['usage']['usage_complete'] = False
    rows[0]['budget']['max_tokens'] *= 2
    report = evaluate_report(rows, manifest)
    assert {'incomplete_usage', 'case_contract_changed'} <= set(report['gate_failures'])


def test_gate_does_not_reward_free_warm_work_while_ignoring_preparation():
    rows, manifest = perfect_rows()
    for row in rows:
        if row['arm'] == 'ore' and row['lifecycle_id']:
            row['work_seconds'] = 350 if row['phase'] == 'cold' else 0
            row['usage']['tokens']['totalTokens'] = 35000 if row['phase'] == 'cold' else 0
    report = evaluate_report(rows, manifest)
    assert report['lifecycle_wall_ratio'] > 1
    assert report['lifecycle_total_token_ratio'] > 1
    assert not report['release_gate_passed']


def test_zero_model_replay_is_valid_but_unobserved_provider_usage_is_not():
    usage = Usage(NEW_BUDGET)
    result = usage.result()
    assert result['usage_complete'] and result['zero_model_execution'] and result['tokens']['totalTokens'] == 0
    usage.calls.append({'usage_observed': False, 'elapsed_seconds': .1})
    result = usage.result()
    assert not result['usage_complete'] and not result['zero_model_execution']


async def test_usage_subtracts_persisted_thread_watermark():
    class Backend:
        on_event = None
        async def run(self, ident, *args, **kwargs):
            self.on_event('thread/tokenUsage/updated', {'threadId': ident, 'tokenUsage': {'total': {'totalTokens': 150, 'inputTokens': 130, 'outputTokens': 20}}})
            return {'turn': {'status': 'completed'}}
    backend = Backend()
    usage = Usage(NEW_BUDGET, {'old-thread': {'totalTokens': 100, 'inputTokens': 90, 'outputTokens': 10}})
    usage.attach(backend)
    await backend.run('old-thread')
    assert usage.result()['tokens']['totalTokens'] == 50
    assert usage.token_count() == 50 and usage.result()['usage_complete']


async def test_shared_native_capacity_and_primitive_budget_are_not_arm_specific():
    fixture = SimpleNamespace(calls=[], saves=[], restart_after_saves=None)
    async def operation(name, args):
        fixture.calls.append(name); await asyncio.sleep(.002); return {}
    fixture.call = operation
    budget = {**NEW_BUDGET, 'max_primitives': 7}
    common = CommonPrimitives(fixture, Usage(budget), budget)
    result = await asyncio.gather(*(common('bench.select', {}) for _ in range(10)), return_exceptions=True)
    assert common.peak == 5 and len(fixture.calls) == 7
    assert sum(isinstance(value, RuntimeError) for value in result) == 3


async def test_workspace_is_persistent_scoped_and_contains_no_expected_answers(tmp_path):
    first = SharedWorkspace(tmp_path / 'first')
    await first.call('bench.workspace_write', {'name': 'program.js', 'text': 'return await tools.bench_fetch({url: origin});'})
    assert (await SharedWorkspace(tmp_path / 'first').call('bench.workspace_read', {'name': 'program.js'}))['text'].startswith('return')
    assert (await SharedWorkspace(tmp_path / 'second').call('bench.workspace_list', {}))['files'] == []
    for name in ('../expected.json', '/tmp/secret', 'nested/a', '..'):
        with pytest.raises(ValueError):
            await first.call('bench.workspace_read', {'name': name})


@pytest.mark.parametrize('family', ['paragraph', 'paginated_list', 'main_and_supplements', 'repeated_extraction', 'restart_failure', 'verification_rate_limit'])
async def test_private_holdout_pair_has_identical_ground_truth_and_fresh_phase(tmp_path, family):
    digests = []
    for arm in ARMS:
        async with EvaluationFixture(family, 0, tmp_path / arm, split='holdout', seed_material=TEST_SEED,
                                     workspace=tmp_path / arm / 'workspace', count_override=8) as fixture:
            digests.append(fixture.contract_digest())
            assert TEST_SEED.decode() not in fixture.prompt
            assert 'expected' not in (await fixture.workspace.call('bench.workspace_list', {}))
            assert not fixture.grade()['quality_pass']
    assert digests[0] == digests[1]
    async with EvaluationFixture(family, 0, tmp_path / 'warm', split='holdout', phase='unchanged', seed_material=TEST_SEED,
                                 workspace=tmp_path / 'warm' / 'workspace', count_override=8) as fixture:
        assert fixture.contract_digest() != digests[0]


async def test_new_verification_grader_enforces_observation_and_server_wait(tmp_path):
    from bs4 import BeautifulSoup
    async with EvaluationFixture('verification_rate_limit', 0, tmp_path / 'case', split='holdout',
                                 seed_material=TEST_SEED, workspace=tmp_path / 'workspace') as fixture:
        await fixture.call('bench.fetch', {'url': fixture.origin + '/'})
        form = await fixture.call('bench.fetch', {'url': fixture.origin + '/access/form'})
        node = BeautifulSoup(form['text'], 'html.parser').select_one('form')
        await fixture.call('bench.verify', {'url': fixture.origin + node['action'], 'code': node['data-code']})
        result = await fixture.call('bench.fetch', {'url': fixture.origin + '/access/form'})
        await fixture.call('bench.save', {'name': 'verified.txt', 'encoding': 'text', 'data': result['text']})
        while True:
            result = await fixture.call('bench.fetch', {'url': fixture.origin + '/limited/content'})
            if result['status'] == 200:
                break
            await fixture.call('bench.wait', {'seconds': float(result['headers']['retry-after'])})
        await fixture.call('bench.save', {'name': 'limited.txt', 'encoding': 'text', 'data': result['text']})
        assert fixture.grade()['quality_pass']


async def test_mocked_one_measures_true_zero_model_and_excludes_grader_from_actor(tmp_path):
    class Actor:
        def __init__(self, state, usage, common):
            self.usage = usage
        async def run(self, fixture):
            source = await fixture.call('bench.fetch', {'url': fixture.origin + '/'})
            selected = await fixture.call('bench.select', {'html': source['text'], 'selector': '#target'})
            await fixture.call('bench.save', {'name': 'paragraph.txt', 'data': selected['values'][0], 'encoding': 'text'})
            return {'status': 'completed', 'first_attempt_passed': True}
        async def close(self):
            pass
    manifest = frozen_manifest()
    row = next(row for row in schedule() if row['family'] == 'paragraph')
    result = await one(row, tmp_path, manifest, TEST_SEED, actor_factory=Actor,
                       fingerprint_fn=lambda: manifest['frozen_fingerprint'])
    assert result['passed'] and result['usage']['zero_model_execution']
    assert result['primitive_counts'] == {'bench.fetch': 1, 'bench.select': 1, 'bench.save': 1}
    assert (tmp_path / 'cases' / row['case_id'] / 'result.json').is_file()


async def test_native_self_generated_replay_needs_no_provider(tmp_path):
    # This is an explicit test program, never supplied to official actors.
    program = {'version': 1, 'steps': [
        {'id': 'page', 'tool': 'bench.fetch', 'inputs': {'url': {'$ref': 'inputs.start_url'}}, 'checks': [{'$eq': [{'$ref': 'output.status'}, 200]}]},
        {'id': 'extract', 'tool': 'bench.select', 'inputs': {'html': {'$ref': 'steps.page.text'}, 'selector': '#target'}},
        {'id': 'save', 'tool': 'bench.save', 'inputs': {'name': 'paragraph.txt', 'data': {'$ref': 'steps.extract.values.0'}, 'encoding': 'text'}}]}
    workspace = tmp_path / 'state' / 'workspace'
    async with EvaluationFixture('paragraph', 0, tmp_path / 'case', phase='unchanged', seed_material=TEST_SEED, workspace=workspace) as fixture:
        await fixture.workspace.call('bench.workspace_write', {'name': 'entrypoint.json', 'text': json.dumps({'schema_version': 'ore.benchmark-entrypoint/v1', 'program': program, 'input': {}})})
        usage = Usage(OLD_BUDGET); common = CommonPrimitives(fixture, usage, OLD_BUDGET); fixture.call = common
        actor = NativeActor(tmp_path / 'state', usage, common)
        async def forbidden_backend():
            raise AssertionError('A reusable program must not force a model turn')
        actor._backend = forbidden_backend
        result = await actor.run(fixture)
        assert result['status'] == 'completed' and fixture.grade()['quality_pass']
        assert usage.result()['zero_model_execution']


def test_prepare_commits_private_seed_but_does_not_freeze_or_run(tmp_path):
    directory = tmp_path / 'cohort'
    manifest = prepare(directory)
    seed = (directory / '.private' / 'holdout.seed').read_bytes()
    assert manifest['seed_commitment'] == hashlib.sha256(seed).hexdigest()
    assert seed.hex() not in json.dumps(manifest)
    assert (directory / '.private' / 'holdout.seed').stat().st_mode & 0o777 == 0o600
    assert not (directory / 'frozen.json').exists() and not (directory / 'cases').exists()
    assert 'same production generic recipe interpreter' in shared_instructions()


async def test_receipt_validator_uses_observed_data_and_not_hidden_answers(tmp_path):
    from ore.models import canonical_digest
    from scripts.benchmark_fixtures_v2 import public_receipt_verifier
    async with EvaluationFixture('paragraph', 0, tmp_path / 'case', seed_material=TEST_SEED, workspace=tmp_path / 'workspace') as fixture:
        receipts = []
        async def observed(name, arguments):
            output = await fixture.call(name, arguments)
            receipts.append({'status': 'completed', 'tool': name, 'input_digest': canonical_digest({'name': name, 'arguments': arguments}), 'output': output})
            return output
        page = await observed('bench.fetch', {'url': fixture.origin + '/'})
        selected = await observed('bench.select', {'html': page['text'], 'selector': '#target'})
        await observed('bench.save', {'name': 'paragraph.txt', 'data': selected['values'][0], 'encoding': 'text'})
        proof = {'receipts': receipts}
        first = public_receipt_verifier(fixture, {'nonce': 1}, proof)
        second = public_receipt_verifier(fixture, {'nonce': 2}, proof)
        assert first['passed'] and first['case_digest'] == second['case_digest']
        fixture._expected = {'an-unrelated-hidden-answer.txt': b'not observed'}
        assert public_receipt_verifier(fixture, {}, proof)['passed']
        assert not fixture.grade()['quality_pass']
        (fixture.output_dir / 'paragraph.txt').write_text('corrupted output')
        assert not public_receipt_verifier(fixture, {}, proof)['passed']


async def test_replay_interruption_resumes_checkpoint_without_duplicate_files(tmp_path):
    program = {'version': 1, 'steps': [
        {'id': 'inventory', 'tool': 'bench.fetch', 'inputs': {'url': {'$ref': 'inputs.start_url'}}},
        {'id': 'links', 'tool': 'bench.select', 'inputs': {'html': {'$ref': 'steps.inventory.text'}, 'selector': 'a[data-output-name]', 'attribute': 'href'}},
        {'id': 'each', 'items': {'$ref': 'steps.links.values'}, 'steps': [
            {'id': 'retry', 'repeat': 3, 'until': {'$eq': [{'$ref': 'steps.page.status'}, 200]}, 'steps': [
                {'id': 'wait', 'tool': 'bench.wait', 'inputs': {'seconds': .06}},
                {'id': 'page', 'tool': 'bench.fetch', 'inputs': {'url': {'$urljoin': [{'$ref': 'inputs.origin'}, {'$ref': 'item'}]}}}]},
            {'id': 'select', 'tool': 'bench.select', 'inputs': {'html': {'$ref': 'steps.retry.page.text'}, 'selector': '[data-measurement]'}},
            {'id': 'save', 'tool': 'bench.save', 'inputs': {'name': {'$format': {'template': 'record-{n:04d}.txt', 'values': {'n': {'$add': [{'$ref': 'index'}, 1]}}}}, 'data': {'$ref': 'steps.select.values.0'}, 'encoding': 'text'}}]}]}
    workspace = tmp_path / 'state' / 'workspace'
    async with EvaluationFixture('restart_failure', 0, tmp_path / 'case', split='holdout', phase='unchanged',
                                 seed_material=TEST_SEED, workspace=workspace, count_override=8) as fixture:
        await fixture.workspace.call('bench.workspace_write', {'name': 'entrypoint.json', 'text': json.dumps({'schema_version': 'ore.benchmark-entrypoint/v1', 'program': program, 'input': {}})})
        usage = Usage(NEW_BUDGET); common = CommonPrimitives(fixture, usage, NEW_BUDGET); fixture.call = common
        actor = NativeActor(tmp_path / 'state', usage, common)
        async def forbidden_backend():
            raise AssertionError('Replay should stay deterministic')
        actor._backend = forbidden_backend
        result = await asyncio.wait_for(actor.run(fixture), 10)
        assert result['restart']['performed'] and result['restart']['receipt']['preserved']
        assert fixture.grade()['quality_pass'] and fixture.grade()['duplicate_writes'] == 0
        assert usage.result()['zero_model_execution']


async def test_candidate_plan_compiles_with_production_engine_without_model_call(tmp_path):
    from scripts.benchmark_architecture_v2 import OreActor
    async with EvaluationFixture('paragraph', 0, tmp_path / 'case', seed_material=TEST_SEED, workspace=tmp_path / 'workspace') as fixture:
        usage = Usage(OLD_BUDGET); common = CommonPrimitives(fixture, usage, OLD_BUDGET); fixture.call = common
        actor = OreActor(tmp_path / 'state', usage, common)
        engine = actor._engine(fixture)
        try:
            plan = actor._plan(fixture)
            engine.workflows.validate_plan(plan)
            assert plan['workflow']['schema_version'] == 'ore.workflow/v2'
            assert engine.recipe_source_contract['fixture_family'] == 'paragraph'
            assert usage.result()['zero_model_execution']
        finally:
            await actor.close()


async def test_baseline_can_test_its_generated_program_during_cold_run(tmp_path):
    program = {'version': 1, 'steps': [
        {'id': 'source', 'tool': 'bench.fetch', 'inputs': {'url': {'$ref': 'inputs.url'}}},
        {'id': 'selection', 'tool': 'bench.select', 'inputs': {'html': {'$ref': 'steps.source.text'}, 'selector': '#target'}},
        {'id': 'save', 'tool': 'bench.save', 'inputs': {'name': 'paragraph.txt', 'data': {'$ref': 'steps.selection.values.0'}, 'encoding': 'text'}}]}
    async with EvaluationFixture('paragraph', 0, tmp_path / 'case', seed_material=TEST_SEED, workspace=tmp_path / 'workspace') as fixture:
        usage = Usage(OLD_BUDGET); common = CommonPrimitives(fixture, usage, OLD_BUDGET); fixture.call = common
        actor = NativeActor(tmp_path / 'state', usage, common)
        class Backend:
            async def thread(self, specs, handler, **kwargs):
                self.names = {spec['name'] for spec in specs}
                assert 'program_execute' in self.names and 'bench_fetch' in self.names
                self.handler = handler
                return 'mock-native-thread'
            async def run(self, ident, prompt, **kwargs):
                result = await self.handler('program_execute', {'program': program, 'inputs': {'url': fixture.origin + '/'}})
                assert result['model_calls'] == 0
                # The same call in the same request uses its own durable receipts.
                await self.handler('program_execute', {'program': program, 'inputs': {'url': fixture.origin + '/'}})
                return {'turn': {'status': 'completed'}, 'text': 'Complete'}
        backend = Backend()
        async def get_backend():
            return backend
        actor._backend = get_backend
        result = await actor._run_native(fixture, fixture.prompt)
        assert result['turn']['status'] == 'completed' and fixture.grade()['quality_pass']
        assert common.admitted == 3 and fixture.grade()['duplicate_writes'] == 0


def test_freeze_snapshots_exact_runtime_and_rejects_manifest_mutation(tmp_path, monkeypatch):
    import scripts.benchmark_architecture_v2 as harness
    monkeypatch.setattr(harness, 'transport_fingerprint', lambda: {'available': True, 'version': 'test-transport', 'sha256': '0' * 64})
    directory = tmp_path / 'cohort'
    harness.prepare(directory)
    frozen = harness.freeze(directory)
    loaded, seed = harness.load_frozen(directory)
    assert loaded['freeze_digest'] == frozen['freeze_digest'] and len(seed) == 32
    runtime = json.loads((directory / 'provenance' / 'runtime.json').read_text())
    for name, checksum in runtime['files'].items():
        assert hashlib.sha256((directory / 'provenance' / 'source' / name).read_bytes()).hexdigest() == checksum
    changed = json.loads((directory / 'frozen.json').read_text())
    changed['gates']['lifecycle_wall_ratio_max'] = 1.2
    (directory / 'frozen.json').write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='Frozen manifest changed'):
        harness.load_frozen(directory)
