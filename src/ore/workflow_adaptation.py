"""Conservative, automatic paired calibration of workflow agent families.

Shadow decisions never execute tools. Promotion needs five independently checked
node outcomes with identical complete action sequences. This is empirical
validation for a pinned task family, not a guarantee of semantic equivalence.
"""
from __future__ import annotations

import contextlib
import asyncio
import json
from .models import canonical_digest

EFFORTS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
MODEL_ORDER = ['gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-6-astra']


def strong_checks(checks):
    """A whole-output equality against approved input/literal is independently testable.

    Shape, nonempty strings, model confidence and comparison to model-generated
    summaries are intentionally insufficient for an automatic downgrade.
    """
    for check in checks:
        if not isinstance(check, dict) or check.get('op') != 'eq': continue
        if check.get('left') != {'$ref': 'output'}: continue
        right = check.get('right')
        if isinstance(right, dict) and '$ref' in right:
            if set(right) == {'$ref'} and str(right['$ref']).startswith('inputs.'): return True
        elif right is not None: return True
    return False


def normalized_spec(spec):
    value = {key: item for key, item in spec.items() if key not in ('id', 'depends_on')}
    # Generated node IDs and foreach indices are execution identities, not task
    # semantics. Item values remain actual calibration cases, never signatures.
    return value


def supported(row):
    return [value['reasoningEffort'] for value in row.get('supportedReasoningEfforts', [])]


class WorkflowAdaptation:
    def __init__(self, manager):
        self.manager, self.store = manager, manager.store

    def signature(self, job, node):
        try:
            from .evaluation import runtime_fingerprint
            runtime = runtime_fingerprint()
        except (ImportError, TypeError):
            runtime = 'workflow-adaptation/v1'
        return canonical_digest({'spec': normalized_spec(node['spec']), 'tools': self.manager._catalog(),
            'runtime': runtime, 'instruction_version': 'workflow-agent/v1',
            'access_profile': job['mission'].get('access_profile'),
            'scope': job['mission'].get('scope'), 'routing': job['mission'].get('routing', {}), 'external_model_content': job['mission'].get('external_model_content')})

    def candidates(self, catalog, baseline, routing=None):
        routing = routing or {}
        rows = {row.get('model', row.get('id')): row for row in catalog}
        result = []
        if isinstance(routing.get('candidates'), list):
            for candidate in routing['candidates']:
                if not isinstance(candidate, dict): continue
                name, effort = candidate.get('model'), candidate.get('effort')
                if name in rows and effort in supported(rows[name]) and (name, effort) != (baseline['model'], baseline['effort']):
                    result.append({'model': name, 'effort': effort})
            return result
        order = routing.get('model_order', MODEL_ORDER)
        if not isinstance(order, list): order = MODEL_ORDER
        baseline_rank = order.index(baseline['model']) if baseline['model'] in order else None
        for name in order:
            if name not in rows: continue
            if baseline_rank is None and name != baseline['model']: continue
            if baseline_rank is not None and order.index(name) > baseline_rank: continue
            efforts = supported(rows[name])
            preferred = ['low', 'medium', 'high'] if name != baseline['model'] else ['medium', 'low']
            for effort in preferred:
                if effort not in efforts: continue
                if name == baseline['model'] and EFFORTS.index(effort) >= EFFORTS.index(baseline['effort']): continue
                result.append({'model': name, 'effort': effort})
                break
        # An unknown but explicitly selected runtime model can still calibrate a
        # supported effort downshift without inventing another model identifier.
        if baseline['model'] not in MODEL_ORDER and baseline['model'] in rows:
            for effort in ['medium', 'low']:
                if effort in supported(rows[baseline['model']]) and EFFORTS.index(effort) < EFFORTS.index(baseline['effort']):
                    result.append({'model': baseline['model'], 'effort': effort}); break
        return result

    def state(self, job, node):
        signature = self.signature(job, node)
        return signature, self.store.get_document('workflow.calibration', signature)

    def choose(self, job, node, catalog, baseline, failures=0):
        if failures or baseline.get('mode') not in ('auto', 'quality_constrained_auto') or not strong_checks(node['spec'].get('checks', [])):
            return baseline
        signature, state = self.state(job, node)
        candidate = state.get('candidate') if state else None
        if state and state.get('verified_pairs', 0) >= 5 and state.get('valid') and candidate in self.candidates(catalog, baseline, job['mission'].get('routing')):
            return {**baseline, **candidate, 'reason': 'five paired, independently checked task-family cases',
                    'calibration_signature': signature, 'task_scoped_validation': True, 'adaptation': 'verified'}
        return {**baseline, 'calibration_signature': signature, 'adaptation': 'quality_baseline'}

    async def shadow(self, task, job, node, catalog, baseline, instructions, prompt, decision, image=None):
        if baseline.get('mode') not in ('auto', 'quality_constrained_auto') or baseline.get('adaptation') == 'verified': return None
        if not strong_checks(node['spec'].get('checks', [])): return None
        signature, state = self.state(job, node)
        choices = self.candidates(catalog, baseline, job['mission'].get('routing'))
        index = state.get('candidate_index', 0) if state else 0
        if index >= len(choices): return None
        candidate = choices[index]
        budget = job['mission'].get('budget', {})
        scope = f'{job["id"]}:{job["revision"]}:{job["generation"]}'
        limit = max(0, int(budget.get('adaptation_max_shadow_turns', min(10, budget.get('max_turns', 100) // 2))))
        if not limit: return None
        if not self.store.reserve_budget(scope, 'adaptation_shadow_turns', 1, limit)['allowed']: return None
        if not self.store.reserve_budget(scope, 'agent_turns', 1, budget.get('max_turns', 100))['allowed']: return None
        from .providers import DECISION_SCHEMA
        backend = self.manager.engine.backend
        tid = None
        try:
            tid = await backend.thread([], None, model=candidate['model'], instructions=instructions)
            self.manager.shadow_threads[task['id']] = tid
            result = await backend.run(tid, prompt, model=candidate['model'], effort=candidate['effort'],
                timeout=min(120, budget.get('max_seconds', 3600)), output_schema=DECISION_SCHEMA, images=[image] if image else None)
            shadow = json.loads(result['text'])
            usage = result.get('usage', result.get('turn', {}).get('usage', {}))
            if budget.get('max_tokens') is not None:
                tokens = usage.get('totalTokens', usage.get('total_tokens', 0))
                if not self.store.reserve_budget(scope, 'model_tokens', tokens, budget['max_tokens'])['allowed']:
                    return {'signature': signature, 'candidate': candidate, 'agree': False, 'reason': 'token_budget'}
            def action(value):
                args = value.get('arguments', {})
                return {'tool': value['tool'], 'arguments': json.loads(args) if isinstance(args, str) else args}
            agree = action(shadow) == action(decision)
            return {'signature': signature, 'candidate': candidate, 'agree': agree, 'candidate_index': index, 'calibration_epoch': state.get('control_epoch', 1) if state else 1,
                    'case_digest': canonical_digest({'input': node.get('resolved_inputs'), 'decision': action(decision)})}
        except Exception as exc:
            return {'signature': signature, 'candidate': candidate, 'agree': False, 'reason': type(exc).__name__, 'candidate_index': index, 'calibration_epoch': state.get('control_epoch', 1) if state else 1}
        finally:
            if tid:
                acknowledgement = None
                with contextlib.suppress(Exception): acknowledgement = await backend.interrupt(tid)
                if asyncio.current_task().cancelling():
                    self.manager.model_interrupts.setdefault(task['id'], []).append(acknowledgement if isinstance(acknowledgement, dict) else
                        {'requested': True, 'acknowledged': None, 'thread_id': tid})
            self.manager.shadow_threads.pop(task['id'], None)

    def record(self, job, node, pairs, *, passed, used_route=None):
        if not pairs and not (used_route and used_route.get('adaptation') == 'verified' and not passed): return
        signature = self.signature(job, node)
        if used_route and used_route.get('adaptation') == 'verified' and not passed:
            old = self.store.get_document('workflow.calibration', signature) or {}
            self.store.put_document('workflow.calibration', signature, {**old, 'signature': signature,
                'valid': False, 'verified_pairs': 0, 'case_digests': [], 'control_epoch': old.get('control_epoch', 1) + 1, 'invalidated_reason': 'independent_check_or_action_failure'})
            return
        if not pairs: return
        if any(pair['signature'] != signature for pair in pairs): return
        candidate = pairs[0]['candidate']
        if any(pair['candidate'] != candidate for pair in pairs): return
        with self.store._tx() as conn:
            previous = self.store._doc(conn, 'workflow.calibration', signature)
            old = previous['data'] if previous else {}
            epoch = old.get('control_epoch', 1)
            if any(pair.get('calibration_epoch', 1) != epoch for pair in pairs): return
            if old and old.get('candidate_index', 0) != pairs[0].get('candidate_index', 0): return
            state = {**old, 'signature': signature, 'candidate': candidate, 'candidate_index': pairs[0].get('candidate_index', 0), 'control_epoch': epoch}
            success = passed and all(pair['agree'] for pair in pairs)
            if not success:
                state.update(valid=False, verified_pairs=0, case_digests=[], candidate_index=state['candidate_index'] + 1, control_epoch=epoch + 1,
                             invalidated_reason='paired_disagreement_or_check_failure')
            else:
                case = canonical_digest([pair.get('case_digest') for pair in pairs])
                cases = list(state.get('case_digests', []))
                if case not in cases: cases.append(case)
                state.update(case_digests=cases[-100:], verified_pairs=len(cases), valid=len(cases) >= 5)
            self.store._put(conn, 'workflow.calibration', signature, state)
