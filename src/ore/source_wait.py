"""Provider-observed quota waits; model text cannot invent a reset or new budget."""
from __future__ import annotations

import math
import time

from .policy import AccessDenied


def source_wait(store, mission, source, operation='search', *, now=None):
    if source not in mission.get('sources', []):
        raise AccessDenied('Source wait is outside the approved source scope')
    now = time.time() if now is None else now
    rows = [row for row in store.list_documents('credential.quota')
            if row.get('source') == source and row.get('operation') == operation
            and row.get('status') in ('rate_limited', 'quota_exhausted')]
    resets = [row['resets_at'] for row in rows if isinstance(row.get('resets_at'), (int, float))
              and math.isfinite(row['resets_at']) and row['resets_at'] > now]
    return {'code': 'source_quota_wait', 'source': source, 'operation': operation,
            'resets_at': min(resets) if resets else None,
            'reason': 'Waiting for the provider-observed quota reset' if resets else 'No future reset reported by provider'}
