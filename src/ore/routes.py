"""Transparent candidate ranking; estimates are explicit, never entitlement proofs."""
from __future__ import annotations

def rank_candidates(candidates,mission,profile=None):
    profile=profile or {};settings=profile.get('retrieval_costs',{})
    allowed=mission.get('scope',{}).get('article_versions',[])
    def assess(candidate):
        c=dict(candidate);version=c.get('version');kind=c.get('source') or c.get('route') or 'unknown'
        estimates=settings.get(kind,{})
        seconds=float(estimates.get('latency_seconds',5 if kind in ('pmc','unpaywall') else 15))
        success=float(estimates.get('verified_probability',0.8 if kind=='pmc' else 0.5))
        fee=float(estimates.get('cost',0))
        mismatch=bool(allowed and version and version not in allowed)
        c['routing']={'eligible':not mismatch,'expected_seconds_per_verified_artifact':round(seconds/max(0.01,min(success,1)),3),
                      'estimated_cost':fee,'version_unknown':version is None,'estimate_source':'access_profile' if estimates else 'uncalibrated_defaults',
                      'reason':'version outside mission' if mismatch else 'candidate requires content and access verification'}
        return c
    rows=[assess(x) for x in candidates]
    return sorted(rows,key=lambda c:(not c['routing']['eligible'],c['routing']['estimated_cost'],c['routing']['expected_seconds_per_verified_artifact']))
