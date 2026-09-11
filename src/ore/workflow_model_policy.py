"""Policy filtering for native tool results before provider transport."""
from __future__ import annotations

_METADATA_STRINGS = {'id', 'artifact_id', 'resource_id', 'receipt_id', 'node_id', 'run_id', 'operation_id',
    'url', 'source_url', 'href', 'doi', 'pmid', 'pmcid', 'title', 'name', 'filename', 'original_name',
    'role', 'status', 'state', 'code', 'media_type', 'content_type', 'sha256', 'digest', 'program_digest',
    'recipe_id', 'contract', 'version', 'kind', 'tool', 'type', 'source', 'provider', 'classification',
    'eligibility', 'identity', 'integrity', 'reason_code', 'control_yield', 'workflow_status'}
_CONTENT_KEYS = {'text', 'body', 'html', 'excerpt', 'abstract', 'content', 'tables', 'pages',
    'body_base64', 'content_base64', 'image_url', 'screenshot', 'screenshots', 'data', 'json_text'}


def model_result(engine, mission, value):
    """Unknown string-bearing source fields are content under metadata policy.

    A blocklist alone would leak encoded bodies or extracted text under `values`.
    Local deterministic programs still consume original results in their journal.
    """
    value = engine.model_observation(mission, value) if hasattr(engine, 'model_observation') else value
    if mission.get('external_model_content', 'selected_page_content') not in ('metadata', 'none'):
        return value
    missing = object()
    def project(item, key=None):
        if key in _CONTENT_KEYS: return missing
        if item is None or isinstance(item, (bool, int, float)): return item
        if isinstance(item, str): return item if key in _METADATA_STRINGS else missing
        if isinstance(item, list):
            values = [project(child, key) for child in item]
            return [child for child in values if child is not missing]
        if isinstance(item, dict):
            result = {}
            for field, child in item.items():
                projected = project(child, field)
                if projected is not missing: result[field] = projected
            return result
        return missing
    result = project(value)
    return {'content_withheld': True, 'policy': 'metadata'} if result is missing else result
