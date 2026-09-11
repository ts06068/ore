"""Transparent candidate ranking; estimates are explicit, never entitlement proofs."""
from __future__ import annotations

def normalize_retrieval_policy(mission, profile=None):
    from .models import RetrievalPolicy
    value = mission.get('retrieval_policy')
    if value is None:
        value = (profile or {}).get('retrieval_policy') or {}
    result = RetrievalPolicy.model_validate(value).model_dump(mode='json')
    if ((profile or {}).get('retrieval_policy') or {}).get('browser_fallback') is False:
        result['browser_fallback'] = False
    return result


def retrieval_plan(mission, profile=None):
    """Executable source admission, independent of authoritative coverage evidence."""
    from .source_policy import normalize_source_policy, authorize_operation, _registry
    profile = profile or {}
    policy = normalize_source_policy(mission, profile)
    routes, skipped = [], []
    registry = _registry()
    for operation in ('search', 'resolve'):
        sources = policy['allow'][operation]
        if '*' in sources:
            sources = [source for source, entry in registry.items() if operation in entry['operations']]
        for source in sources:
            admission = authorize_operation(mission, profile, source, operation)
            row = {key: admission[key] for key in ('source', 'operation', 'code', 'required')}
            row['readiness'] = admission['readiness']['state']
            (routes if admission['allowed'] else skipped).append(row)
    # Verified available APIs first; unknown means eligible to probe, never ready.
    routes.sort(key=lambda row: (row['operation'] != 'search', row['readiness'] != 'ready', row['source']))
    return {'policy': normalize_retrieval_policy(mission, profile), 'available': routes, 'skipped': skipped,
            'authority_order': ['journal', 'publisher', 'pmc'],
            'inventory_verified_by_routing': False, 'global_recall': 'unknown'}


def rank_candidates(candidates,mission,profile=None):
    profile=profile or {};settings=profile.get('retrieval_costs',{})
    allowed=mission.get('scope',{}).get('article_versions',[])
    retrieval=normalize_retrieval_policy(mission,profile)
    def assess(candidate):
        c=dict(candidate);version=c.get('version');kind=c.get('source') or c.get('route') or 'unknown'
        estimates=settings.get(kind,{})
        seconds=float(estimates.get('latency_seconds',5 if kind in ('pmc','unpaywall') else 15))
        success=float(estimates.get('verified_probability',0.8 if kind=='pmc' else 0.5))
        fee=float(estimates.get('cost',0))
        mismatch=bool(allowed and version and version not in allowed)
        from .source_policy import authorize_operation, source_for_url
        admission=authorize_operation(mission,profile,source_for_url(c.get('url',''),profile,hint=kind),'download',url=c.get('url'))
        c['routing']={'eligible':not mismatch and admission['allowed'],'expected_seconds_per_verified_artifact':round(seconds/max(0.01,min(success,1)),3),
                      'estimated_cost':fee,'version_unknown':version is None,'estimate_source':'access_profile' if estimates else 'uncalibrated_defaults',
                      'reason':'version outside mission' if mismatch else admission['code'] if not admission['allowed'] else 'candidate requires content and access verification',
                      'retrieval_mode':retrieval['mode']}
        return c
    rows=[assess(x) for x in candidates]
    def order(c):
        authority = c.get('authority') or c.get('source') or 'unknown'
        tier = {'journal':0, 'publisher':1, 'pmc':2}.get(authority, 3)
        return (not c['routing']['eligible'], tier if retrieval['mode']=='official_first' else 0,
                c['routing']['estimated_cost'],c['routing']['expected_seconds_per_verified_artifact'])
    return sorted(rows,key=order)
