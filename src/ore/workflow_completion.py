"""Completion evidence obtained from durable operations, never model assertions."""
from __future__ import annotations

import copy


def successful(receipt):
    if receipt.get('status') != 'completed':
        return False
    value = receipt.get('output')
    if isinstance(value, dict):
        if value.get('error') or value.get('allowed') is False or value.get('needs_user'):
            return False
        if isinstance(value.get('status'), int) and value['status'] >= 400:
            return False
    return True


def scoped_receipts(store, run, node):
    prefix = node['id'] + '/'
    return [receipt for receipt in store.list_documents('workflow.operation', run['job_id'])
            if receipt.get('run_id') == run['id'] and
            (receipt.get('node_id') == node['id'] or str(receipt.get('node_id', '')).startswith(prefix))]


def receipt_output(value, receipts):
    """Resolve explicit receipt handles only against this node and descendants."""
    by_id = {row['id']: row for row in receipts if successful(row)}
    def walk(item):
        if isinstance(item, dict):
            if '$receipt' in item:
                if set(item) - {'$receipt', 'path'} or item['$receipt'] not in by_id:
                    raise ValueError('Completion references an unavailable successful receipt')
                result = copy.deepcopy(by_id[item['$receipt']]['output'])
                path = item.get('path', '')
                if not isinstance(path, str):
                    raise ValueError('Receipt path must be a string')
                for part in path.split('.') if path else []:
                    result = result[int(part)] if isinstance(result, list) else result[part]
                return result
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item
    return walk(value)


def completion_evidence(manager, run, node, output):
    from .workflow import NodeOutcome
    receipts = scoped_receipts(manager.store, run, node)
    good = [row for row in receipts if successful(row)]
    output = receipt_output(output, receipts)
    contract = node['spec'].get('completion', {})
    if not isinstance(contract, dict):
        raise ValueError('completion must be an object')
    if not good and not contract.get('pure_planning', False):
        raise NodeOutcome('needs_replan', {'code': 'receipt_evidence_missing', 'reason': 'No successful scoped operation receipt supports completion'})
    for required in contract.get('required_tools', []):
        matching = [receipt for receipt in good if receipt['tool'] == required['tool']]
        passed = []
        for receipt in matching:
            try:
                manager._check(required.get('checks', []), {'output': receipt['output'], 'receipt': receipt})
                passed.append(receipt)
            except (ValueError, TypeError, KeyError):
                pass
        if len(passed) < int(required.get('min_count', 1)):
            raise NodeOutcome('needs_replan', {'code': 'receipt_check_failed', 'tool': required['tool'],
                                               'required': int(required.get('min_count', 1)), 'actual': len(passed)})
    # Match artifacts to trusted operation outputs. Unrelated previous artifacts
    # from this job cannot satisfy a new agent's independent completion checks.
    identities = set()
    def collect(value):
        if isinstance(value, dict):
            for key in ('artifact_id', 'id', 'sha256'):
                if isinstance(value.get(key), str): identities.add(value[key])
            for child in value.values(): collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    for receipt in good: collect(receipt['output'])
    artifacts = [artifact for artifact in manager.store.artifacts(run['job_id'])
                 if artifact.get('id') in identities or artifact.get('sha256') in identities]
    for required in contract.get('artifact_requirements', []):
        matching = [artifact for artifact in artifacts if artifact.get('status') == 'verified' and
                    artifact.get('integrity') != 'invalid' and
                    all(artifact.get(key) == required[key] for key in ('role', 'resource_id', 'media_type', 'sha256') if key in required)]
        passed = []
        for artifact in matching:
            try:
                manager._check(required.get('checks', []), {'output': artifact, 'artifact': artifact})
                passed.append(artifact)
            except (ValueError, TypeError, KeyError):
                pass
        if len(passed) < int(required.get('min_count', 1)):
            raise NodeOutcome('needs_replan', {'code': 'artifact_check_failed', 'role': required.get('role'),
                                               'required': int(required.get('min_count', 1)), 'actual': len(passed)})
    strength = {'schema': bool(node['spec'].get('checks')), 'receipt': bool(good),
                'independent_goal': bool(contract.get('required_tools') or contract.get('artifact_requirements')),
                'corpus': False}
    return output, {'validation_strength': strength, 'receipt_ids': [row['id'] for row in good],
                    'artifact_ids': [row['id'] for row in artifacts],
                    'note': 'Workflow completion does not establish corpus completeness; coverage audit remains authoritative.'}
