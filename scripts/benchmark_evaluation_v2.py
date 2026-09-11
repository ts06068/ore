"""Preregistered schedule and fail-closed finite comparison gates.

No model, source or filesystem side effects occur in this module. A passing gate
is an engineering result for this finite cohort, never a universal guarantee.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math

try:
    from .benchmark_fixtures_v2 import FAMILIES, LIFECYCLE_FAMILIES, SCALES
except ImportError:
    from benchmark_fixtures_v2 import FAMILIES, LIFECYCLE_FAMILIES, SCALES

SCHEMA = 'ore.architecture-comparison/v2'
ARMS = ('standalone', 'ore')
MODEL, EFFORT = 'gpt-6-astra', 'high'
OLD_BUDGET = {'max_turns': 80, 'max_seconds': 300, 'max_tokens': 500_000,
              'max_bytes': 20_000_000, 'max_agent_workers': 5, 'max_tasks': 300, 'max_primitives': 180}
NEW_BUDGET = {'max_turns': 160, 'max_seconds': 900, 'max_tokens': 1_000_000,
              'max_bytes': 256 * 1024 * 1024, 'max_agent_workers': 5, 'max_tasks': 20_000, 'max_primitives': 20_000}
GATES = {'ore_full_passes_required': 36, 'baseline_success_ore_failure_allowed': 0,
         'lifecycle_wall_ratio_max': 0.80, 'lifecycle_total_token_ratio_max': 0.80,
         'each_lifecycle_family_wall_ratio_max': 1.00, 'each_lifecycle_family_token_ratio_max': 1.00,
         'lifecycle_all_arms_must_pass': True, 'usage_complete_required': True,
         'universal_superiority_claim': False, 'statistical_superiority_claim': False}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def schedule():
    rows = []
    def add(split, family, repetition, phase):
        offset = (FAMILIES.index(family) + repetition + ('cold', 'unchanged', 'changed').index(phase)) % 2
        order = ARMS[offset:] + ARMS[:offset]
        pair_id = f'{split}-{family}-{repetition}-{phase}'
        lifecycle = f'holdout-{family}-0' if split == 'holdout' and repetition == 0 and family in LIFECYCLE_FAMILIES else None
        for arm in order:
            rows.append({'case_id': f'{pair_id}-{arm}', 'pair_id': pair_id, 'split': split,
                         'family': family, 'repetition': repetition, 'phase': phase, 'arm': arm,
                         'lifecycle_id': lifecycle, 'budget': deepcopy(OLD_BUDGET if split == 'regression' else NEW_BUDGET),
                         'source_items': SCALES[family] if split == 'holdout' else None})
    for rep in range(3):
        for family in FAMILIES:
            add('regression', family, rep, 'cold')
    for rep in range(2):
        for family in FAMILIES:
            add('holdout', family, rep, 'cold')
    for phase in ('unchanged', 'changed'):
        for family in LIFECYCLE_FAMILIES:
            add('holdout', family, 0, phase)
    assert len(rows) == 72
    return rows


def preregistration(seed_commitment):
    return {'schema_version': SCHEMA, 'stage': 'preregistered', 'model': MODEL, 'effort': EFFORT,
            'expected_runs': 72, 'case_concurrency': 1, 'within_case_concurrency': 5,
            'arms': {'standalone': 'Native Codex with persistent thread/workspace and self-generated generic replay',
                     'ore': 'Production ORE native workflow and recipe policy with the same raw primitives'},
            'seed_commitment': seed_commitment, 'schedule': schedule(), 'gates': deepcopy(GATES),
            'workload_accounting': 'Cold + unchanged-data-request + changed-layout-request; preparation, validation, failed attempts, replay, repair and fallback all included.',
            'unchanged_phase_means': 'Source layout is unchanged; requested data and output directory are fresh.',
            'cache_policy': 'Each phase uses fresh source data/output directory. Both actors retain their own thread and generated workspace; provider cached-input tokens are reported separately.',
            'holdout_policy': 'Official secret seed is generated before freeze, committed here, and never included in any actor prompt/tool/workspace. Runtime changes after freeze abort the cohort without selective reruns.',
            'invalid_run_policy': 'All started cases retained. Runtime/fixture/tool drift invalidates the cohort. Provider/source failures and timeouts are scored, not silently retried. No result-dependent sample extension.',
            'limitations': ['Known old fixtures are regression data, not independent evidence.',
                           'Two cold seeds per new family and one lifecycle seed per selected family do not establish statistical or universal superiority.',
                           'Local fixtures do not measure journal access, Cloudflare clearance, global recall or invoice cost.',
                           'Token ceilings are observed and checked at calls/events; a provider turn may overshoot before interruption completes. Overshoot fails the budget gate.',
                           'No baseline replay is precomputed or copied from ORE; both actors may generate and retain their own programs.']}


def _tokens(row):
    usage = row.get('usage') or {}
    if not usage.get('usage_complete'):
        return None
    tokens = usage.get('tokens') or {}
    value = tokens.get('totalTokens', tokens.get('total_tokens'))
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and math.isfinite(value) else None


def _ratio(left, right):
    if right == 0:
        return 1.0 if left == 0 else None
    return left / right


def _seconds(row):
    value = row.get('work_seconds')
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and math.isfinite(value) else None


def evaluate_report(rows, manifest):
    expected = {row['case_id']: row for row in manifest['schedule']}
    counts = Counter(row.get('case_id') for row in rows)
    reasons = []
    if any(count != 1 for count in counts.values()):
        reasons.append('duplicate_case_results')
    if set(counts) != set(expected):
        reasons.append('cohort_incomplete_or_unregistered_cases')
    before = manifest.get('frozen_fingerprint')
    if not before:
        reasons.append('runtime_not_frozen')
    for row in rows:
        registered = expected.get(row.get('case_id'))
        if registered and any(row.get(key) != registered.get(key) for key in ('pair_id', 'split', 'family', 'repetition', 'phase', 'arm', 'lifecycle_id', 'budget')):
            reasons.append('case_contract_changed')
        if row.get('fingerprint_start') != before or row.get('fingerprint_end') != before:
            reasons.append('runtime_changed_or_unmeasured')
        if row.get('preregistration_digest') != manifest.get('preregistration_digest'):
            reasons.append('preregistration_digest_mismatch')
        for call in row.get('usage', {}).get('calls', []):
            if call.get('model') != manifest['model'] or call.get('effort') != manifest['effort']:
                reasons.append('pinned_model_or_effort_changed')
        if _tokens(row) is None:
            reasons.append('incomplete_usage')
        if _seconds(row) is None:
            reasons.append('incomplete_elapsed_measurement')
    paired = defaultdict(dict)
    family_counts = defaultdict(lambda: {arm: {'runs': 0, 'passed': 0} for arm in ARMS})
    for row in rows:
        paired[row.get('pair_id')][row.get('arm')] = row
        if row.get('arm') in ARMS:
            value = family_counts[row.get('family')][row['arm']]
            value['runs'] += 1; value['passed'] += int(row.get('passed') is True)
    discordant = []
    for pair, arms in paired.items():
        if set(arms) != set(ARMS):
            continue
        if arms['standalone'].get('passed') is True and arms['ore'].get('passed') is not True:
            discordant.append(pair)
        if arms['standalone'].get('grade', {}).get('source_contract_digest') != arms['ore'].get('grade', {}).get('source_contract_digest'):
            reasons.append('paired_source_contract_differs')
        for contract in ('tool_contract_digest', 'semantic_request_digest'):
            if not arms['standalone'].get(contract) or arms['standalone'].get(contract) != arms['ore'].get(contract):
                reasons.append('paired_' + contract + '_differs_or_missing')
        if not arms['standalone'].get('grade', {}).get('source_contract_digest'):
            reasons.append('source_contract_unmeasured')
    if discordant:
        reasons.append('baseline_success_ore_failure')
    ore = [row for row in rows if row.get('arm') == 'ore']
    if sum(row.get('passed') is True for row in ore) != GATES['ore_full_passes_required']:
        reasons.append('ore_quality_release_gate_failed')
    lifecycle = {}
    for family in LIFECYCLE_FAMILIES:
        totals = {}
        for arm in ARMS:
            cases = [row for row in rows if row.get('family') == family and row.get('arm') == arm and row.get('lifecycle_id')]
            valid = (len(cases) == 3 and {row['phase'] for row in cases} == {'cold', 'unchanged', 'changed'}
                     and all(row.get('passed') is True and _tokens(row) is not None and _seconds(row) is not None for row in cases))
            if not valid:
                reasons.append('lifecycle_requires_complete_successful_triplets')
            totals[arm] = {'valid': valid, 'work_seconds': sum(_seconds(row) or 0 for row in cases),
                           'total_tokens': sum(_tokens(row) or 0 for row in cases), 'cases': [row.get('case_id') for row in cases]}
        valid = all(item['valid'] for item in totals.values())
        wall = _ratio(totals['ore']['work_seconds'], totals['standalone']['work_seconds']) if valid else None
        tokens = _ratio(totals['ore']['total_tokens'], totals['standalone']['total_tokens']) if valid else None
        lifecycle[family] = {'arms': totals, 'wall_ratio': wall, 'total_token_ratio': tokens}
        if wall is None or wall > GATES['each_lifecycle_family_wall_ratio_max']:
            reasons.append('lifecycle_family_wall_regression_or_unknown')
        if tokens is None or tokens > GATES['each_lifecycle_family_token_ratio_max']:
            reasons.append('lifecycle_family_token_regression_or_unknown')
    valid_lifecycle = all(all(item['valid'] for item in value['arms'].values()) for value in lifecycle.values())
    wall_ratio = _ratio(sum(v['arms']['ore']['work_seconds'] for v in lifecycle.values()),
                        sum(v['arms']['standalone']['work_seconds'] for v in lifecycle.values())) if valid_lifecycle else None
    token_ratio = _ratio(sum(v['arms']['ore']['total_tokens'] for v in lifecycle.values()),
                         sum(v['arms']['standalone']['total_tokens'] for v in lifecycle.values())) if valid_lifecycle else None
    if wall_ratio is None or wall_ratio > GATES['lifecycle_wall_ratio_max']:
        reasons.append('lifecycle_wall_improvement_below_20_percent_or_unknown')
    if token_ratio is None or token_ratio > GATES['lifecycle_total_token_ratio_max']:
        reasons.append('lifecycle_token_improvement_below_20_percent_or_unknown')
    arms = {}
    for arm in ARMS:
        group = [row for row in rows if row.get('arm') == arm]
        token_totals = Counter()
        for row in group:
            token_totals.update({key: value for key, value in (row.get('usage', {}).get('tokens') or {}).items()
                                 if isinstance(value, (int, float)) and not isinstance(value, bool)})
        arms[arm] = {'runs': len(group), 'full_passes': sum(row.get('passed') is True for row in group),
                     'observed_tokens': dict(token_totals), 'usage_complete_runs': sum(_tokens(row) is not None for row in group),
                     'work_seconds_all_runs': sum(_seconds(row) or 0 for row in group),
                     'fallback_runs': sum(bool(row.get('fallbacks')) for row in group),
                     'first_attempt_failures': sum(row.get('first_attempt_passed') is False for row in group),
                     'zero_model_runs': sum(row.get('usage', {}).get('zero_model_execution') is True for row in group)}
    reasons = sorted(set(reasons))
    return {'schema_version': SCHEMA, 'expected_runs': len(expected), 'completed_runs': len(rows),
            'comparison_complete': set(counts) == set(expected) and all(count == 1 for count in counts.values()),
            'release_gate_passed': not reasons, 'gate_failures': reasons, 'gates': deepcopy(GATES),
            'arms': arms, 'family_quality': dict(family_counts), 'baseline_success_ore_failures': discordant,
            'lifecycle': lifecycle, 'lifecycle_wall_ratio': wall_ratio, 'lifecycle_total_token_ratio': token_ratio,
            'claim_scope': 'Finite preregistered tasks under this frozen runtime only; not statistical/universal superiority.',
            'invoice_cost': None, 'runs': rows}
