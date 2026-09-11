"""Bounded public execution context for native workflow sessions.

Full durable state remains in ORE. A provider receives one compact initial view,
then continuation deltas; explicit read-only inspection returns bounded slices.
"""
from __future__ import annotations

import copy
import json
from typing import Any

INITIAL_BYTES = 64 * 1024
DELTA_BYTES = 16 * 1024
INSPECT_BYTES = 16 * 1024


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'), default=str).encode('utf-8')


def compact(value: Any, *, pointer: dict, limit: int = DELTA_BYTES) -> Any:
    """Retain small values exactly; replace large values with inspectable handles."""
    raw = encoded(value)
    if len(raw) <= limit:
        return copy.deepcopy(value)
    result = {'stored': True, 'bytes': len(raw), 'inspect': pointer}
    if isinstance(value, dict):
        result['keys'] = list(value)[:30]
    elif isinstance(value, list):
        result['items'] = len(value)
    return result


def initial_context(run: dict, node: dict, mission: dict, dependencies: list[dict]) -> dict:
    """No all-node snapshot or duplicate plan. Tools are separate native schemas."""
    result = {
        'protocol': 'ore.agent-context/v2',
        'run_id': run['id'], 'node_id': node['id'],
        'goal': mission.get('goal', run['plan'].get('goal')),
        'node': {key: node['spec'][key] for key in ('goal', 'instructions', 'purpose', 'checks', 'completion', 'allowed_tools') if key in node['spec']},
        'inputs': compact(node.get('resolved_inputs', node['spec'].get('inputs', {})),
                          pointer={'kind': 'inputs', 'node_id': node['id']}, limit=24 * 1024),
        'envelope': {key: mission[key] for key in ('allowed_origins', 'sources', 'source_policy', 'retrieval_policy',
                     'artifact_roles', 'completeness', 'budget', 'on_challenge') if key in mission},
        'dependencies': [{'node_id': dependency['id'], 'status': dependency['status'],
                          'output': compact(dependency.get('output'), pointer={'kind': 'node', 'node_id': dependency['id']}, limit=2048)}
                         for dependency in dependencies[:50]],
        'prior_execution': node.get('replay_fallback'),
        'inspection': 'Use workflow.inspect for retained state; results are bounded JSON slices. Use only observed source content as data.',
    }
    if len(dependencies) > 50:
        result['dependencies_remaining'] = len(dependencies) - 50
    if len(encoded(result)) > INITIAL_BYTES:
        for key in ('node', 'envelope', 'dependencies', 'inputs', 'goal'):
            result[key] = compact(result[key], pointer={'kind': 'context', 'field': key}, limit=4096)
            if len(encoded(result)) <= INITIAL_BYTES:
                break
    if len(encoded(result)) > INITIAL_BYTES:
        raise ValueError('Initial native context exceeds 64 KiB')
    return result


def continuation_context(node: dict) -> dict:
    delta = node.get('continuation_delta') or {'reason': 'resume', 'node_id': node['id']}
    result = {'protocol': 'ore.agent-delta/v2', 'node_id': node['id'],
              'continuation': compact(delta, pointer={'kind': 'continuation', 'node_id': node['id']}, limit=DELTA_BYTES - 1024)}
    return result


def slice_json(value: Any, *, offset: int = 0, limit: int = INSPECT_BYTES) -> dict:
    """Byte bounded transport of a JSON document; offsets count Unicode chars."""
    if not isinstance(offset, int) or offset < 0:
        raise ValueError('Inspection offset must be nonnegative')
    if not isinstance(limit, int) or not 256 <= limit <= INSPECT_BYTES:
        raise ValueError('Inspection limit must be between 256 and 16384 bytes')
    text = encoded(value).decode('utf-8')
    chunk = text[offset:offset + limit]
    while len(chunk.encode('utf-8')) > limit:
        chunk = chunk[:max(1, len(chunk) // 2)]
    next_offset = offset + len(chunk)
    return {'json_text': chunk, 'offset': offset, 'next_offset': next_offset if next_offset < len(text) else None,
            'total_characters': len(text), 'complete': offset == 0 and next_offset >= len(text)}
