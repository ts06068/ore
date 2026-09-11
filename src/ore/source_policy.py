"""Operation-specific source admission and recoverable, secret-free failures.

Readiness is access-profile evidence, not a property inferred from a provider name.
An explicit empty allowlist denies that operation. Exclusions always win.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import os
from urllib.parse import urlsplit

OPERATIONS = ('search', 'resolve', 'browser', 'download', 'import')
ALIASES = {'wos_starter': 'wos', 'kci_oai': 'kci', 'googlescholar': 'google_scholar', 'scholar': 'google_scholar', 'jama_cardiology': 'jama-cardiology'}
OP_ALIASES = {'export_import': 'import', 'import_links': 'import', 'api_search': 'search', 'api_resolve': 'resolve', 'fetch': 'download'}
STATES = {'ready', 'unknown', 'unconfigured', 'approval_pending', 'auth_required', 'entitlement_denied', 'rate_limited', 'temporarily_unavailable', 'unsupported'}
HOSTS = {'webofscience.com': 'wos', 'clarivate.com': 'wos', 'scopus.com': 'scopus', 'api.elsevier.com': 'scopus',
         'pubmed.ncbi.nlm.nih.gov': 'pubmed', 'eutils.ncbi.nlm.nih.gov': 'pubmed', 'pmc.ncbi.nlm.nih.gov': 'pmc',
         'pmc-oa-opendata.s3.amazonaws.com': 'pmc', 'api.crossref.org': 'crossref', 'api.unpaywall.org': 'unpaywall',
         'kci.go.kr': 'kci', 'scienceon.kisti.re.kr': 'scienceon', 'dbpia.co.kr': 'dbpia', 'kiss.kstudy.com': 'kiss',
         'riss.kr': 'riss', 'scholar.google.com': 'google_scholar', 'jacc.org': 'jacc', 'ahajournals.org': 'circulation',
         'academic.oup.com': 'ehj', 'jamanetwork.com': 'jama-cardiology', 'e-hir.org': 'hir', 'journals.plos.org': 'plos-medicine'}


def canonical_source(source):
    value = str(source).strip().lower()
    return ALIASES.get(value, value)


def canonical_operation(operation):
    value = OP_ALIASES.get(operation, operation)
    if value not in OPERATIONS:
        raise ValueError('Unsupported source operation')
    return value


def source_for_url(url, profile=None, *, hint=None):
    part = urlsplit(url)
    host = (part.hostname or '').lower()
    if host == 'api.clarivate.com' and part.path.startswith('/api/wos'):return 'wos_expanded'
    if host == 'apigateway.kisti.re.kr':return 'scienceon'
    if host in ('www.ncbi.nlm.nih.gov','ncbi.nlm.nih.gov') and '/pmc/' in part.path:return 'pmc'
    configured = (profile or {}).get('source_origins', {})
    mapping = {**HOSTS, **configured}
    for origin, source in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        domain = (urlsplit(origin).hostname if '://' in origin else origin).lower()
        if host == domain or host.endswith('.' + domain):
            return canonical_source(source)
    return canonical_source(hint) if hint else 'general_web'


def operation_for_url(url, default_operation):
    """Preserve API semantics even when reached through fetch or a browser."""
    part = urlsplit(url)
    host, path = (part.hostname or '').lower(), part.path.lower()
    if host == 'api.unpaywall.org' or host == 'www.ncbi.nlm.nih.gov' and '/pmc/utils/' in path:
        return 'resolve'
    if host in ('api.crossref.org', 'eutils.ncbi.nlm.nih.gov', 'api.clarivate.com', 'wos-api.clarivate.com', 'api.elsevier.com'):
        return 'search'
    if host.endswith('.kci.go.kr') or host == 'kci.go.kr':
        if '/oai/' in path or '/openapi/' in path:return 'search'
    if host == 'api.dbpia.co.kr' or host.endswith('dbpia.co.kr') and ('/openapi/' in path or '/piaapi/' in path):return 'search'
    if host == 'apigateway.kisti.re.kr' or host.endswith('kisti.re.kr') and ('/openapi/' in path or '/api/' in path):return 'search'
    # PMC metadata exports resolve full text; actual article/supplement assets are downloads.
    if host in ('pmc.ncbi.nlm.nih.gov', 'www.ncbi.nlm.nih.gov') and ('/idconv/' in path or '/oa.fcgi' in path):return 'resolve'
    return canonical_operation(default_operation)


def _lists(value):
    if not isinstance(value, dict):
        raise ValueError('Source operation policies must be objects')
    result = {}
    for operation, sources in value.items():
        operation = canonical_operation(operation)
        if not isinstance(sources, list) or any(not isinstance(s, str) or not s.strip() for s in sources):
            raise ValueError('Source allow/exclude entries must be lists of nonempty source IDs')
        result[operation] = sorted(set(canonical_source(s) for s in sources))
    return result


def normalize_source_policy(mission, profile=None):
    profile = profile or {}
    source_list = mission.get('sources', [])
    if not isinstance(source_list, list):
        raise ValueError('mission.sources must be an explicit list')
    defaults = {'search': [canonical_source(s) for s in source_list], 'resolve': ['pmc', 'unpaywall'],
                'browser': ['*'], 'download': ['*'], 'import': ['*']}
    configured = mission.get('source_policy')
    if configured is not None and not isinstance(configured, dict):
        raise ValueError('source_policy must be an object')
    configured = configured or {}
    allow = {**defaults, **_lists(configured.get('allow', {}))}
    exclude = _lists(configured.get('exclude', {}))
    required = _lists(configured.get('required', {}))
    profile_policy = profile.get('source_policy') or {}
    profile_allow = _lists(profile_policy.get('allow', {}))
    for operation, values in profile_allow.items():
        requested = allow[operation]
        allow[operation] = values if '*' in requested else requested if '*' in values else sorted(set(requested) & set(values))
    for operation, values in _lists(profile_policy.get('exclude', {})).items():
        exclude[operation] = sorted(set(exclude.get(operation, [])) | set(values))
    for source, config in profile.get('sources', {}).items():
        if isinstance(config, dict) and config.get('enabled') is False:
            for operation in OPERATIONS:
                exclude[operation] = sorted(set(exclude.get(operation, [])) | {canonical_source(source)})
    return {'schema_version': 'ore.source-policy/v1', 'allow': allow, 'exclude': exclude, 'required': required}


def _registry():
    try:
        from ore_scholarly.registry import SOURCES
        return {item['id']: item for item in SOURCES}
    except ImportError:
        return {}


def source_readiness(source, operation, profile=None):
    source, operation = canonical_source(source), canonical_operation(operation)
    profile = profile or {}
    entry = _registry().get(source)
    supported = entry is None or operation in entry.get('operations', []) or operation in ('browser', 'download', 'import')
    config = profile.get('sources', {}).get(source, {})
    configured = True
    if entry and operation in ('search', 'resolve'):
        for name, env in entry.get('credentials', {}).items():
            configured &= bool(config.get(name + '_ref') or os.environ.get(config.get(name + '_env', env)))
    saved = deepcopy(profile.get('source_readiness', {}).get(source, {}).get(operation, {}))
    state = saved.get('state', 'unknown' if configured else 'unconfigured')
    if not supported:
        state = 'unsupported'
    elif state not in STATES:
        state = 'unknown'
    elif operation in ('search', 'resolve') and not configured and state not in ('approval_pending', 'entitlement_denied'):
        state = 'unconfigured'
    if state == 'ready' and (not saved.get('evidence_ref') or not saved.get('observed_at')):
        state = 'unknown'
    if state == 'ready' and saved.get('expires_at'):
        try:
            if datetime.fromisoformat(saved['expires_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                state = 'unknown'
        except (ValueError, TypeError):
            state = 'unknown'
    # Never return arbitrary profile fields, credential references or values.
    return {'source': source, 'operation': operation, 'state': state, 'implemented': supported,
            'configured': configured if operation in ('search', 'resolve') else None,
            **{key: saved[key] for key in ('observed_at', 'evidence_ref', 'expires_at', 'retry_after', 'scope') if key in saved}}


def authorize_operation(mission, profile, source, operation, *, url=None):
    source, operation = canonical_source(source), operation_for_url(url, operation) if url else canonical_operation(operation)
    policy = normalize_source_policy(mission, profile)
    required = source in policy['required'].get(operation, []) or '*' in policy['required'].get(operation, [])
    forbidden = policy['exclude'].get(operation, [])
    permitted = policy['allow'].get(operation, [])
    readiness = source_readiness(source, operation, profile)
    code = 'allowed'
    if source in forbidden or '*' in forbidden:
        code = 'source_excluded'
    elif source not in permitted and '*' not in permitted:
        code = 'source_not_allowed'
    elif operation == 'browser' and any((config.get('retrieval_policy') or {}).get('browser_fallback') is False for config in (mission, profile)):
        code = 'browser_fallback_disabled'
    elif readiness['state'] not in ('ready', 'unknown'):
        code = readiness['state']
    if url:
        mapped = source_for_url(url, profile, hint=source)
        # A hint cannot disguise a known excluded provider behind general_web.
        if mapped != source:
            linked = authorize_operation(mission, profile, mapped, operation)
            if not linked['allowed']:
                code = linked['code']
    return {'allowed': code == 'allowed', 'source': source, 'operation': operation, 'code': code,
            'reason': 'Source operation permitted; unknown availability still requires observation' if code == 'allowed' else code.replace('_', ' '),
            'required': required, 'recoverable': code != 'allowed', 'retryable': code in ('rate_limited', 'temporarily_unavailable'),
            'retry_after': readiness.get('retry_after'), 'fallback_allowed': code != 'allowed', 'readiness': readiness}


class SourceUnavailable(ValueError):
    def __init__(self, result):
        self.result = result
        self.code = result['code']
        super().__init__(result.get('reason', self.code))
    def to_dict(self):
        return {'error': True, **self.result}


def require_operation(mission, profile, source, operation, *, url=None):
    result = authorize_operation(mission, profile, source, operation, url=url)
    if not result['allowed']:
        raise SourceUnavailable(result)
    return result


def scholarly_failure(exc, source, operation):
    if isinstance(exc, SourceUnavailable):
        return exc.to_dict()
    code = getattr(exc, 'code', 'source_operation_failed')
    state = {'credentials_missing': 'unconfigured', 'access_required': 'auth_required', 'transport_error': 'temporarily_unavailable',
             'provider_http_error': 'temporarily_unavailable', 'operation_unsupported': 'unsupported'}.get(code, code)
    return {'error': True, 'allowed': False, 'source': canonical_source(source), 'operation': canonical_operation(operation),
            'code': code, 'state': state, 'recoverable': True, 'retryable': state in ('rate_limited', 'temporarily_unavailable'),
            'retry_after': getattr(exc, 'retry_after', None), 'fallback_allowed': True,
            'reason': f'{canonical_source(source)} {operation}: {code}', 'http_status': getattr(exc, 'status', None)}


def source_catalog(profile=None, secret_resolver=None):
    try:
        from ore_scholarly import list_sources
    except ImportError:
        return []
    profile = profile or {}
    config = {'sources': profile.get('sources', {})}
    if secret_resolver:
        config['secret_resolver'] = secret_resolver
    result = []
    for item in list_sources(config):
        supported = [canonical_operation(op) for op in item['operations']]
        readiness = {op: source_readiness(item['id'], op, profile) for op in sorted(set(supported))}
        result.append({**item, 'operation_readiness': readiness, 'readiness': readiness,
                       'live_verified': any(row['state'] == 'ready' for row in readiness.values()),
                       'live_verification': {op: row.get('evidence_ref') for op, row in readiness.items() if row['state'] == 'ready'}})
    return result


def audit_required_operations(store, job_id):
    """Required operations need current-revision executor evidence, not readiness claims.

    Successful search use does not assert that an entire database was exhausted.
    Official identity manifests establish the collection denominator separately.
    """
    job=store.get_job(job_id)
    policy=normalize_source_policy(job['mission'])
    observations=[o for o in store.observations(job_id) if o.get('revision')==job['revision'] and o.get('generation')==job['generation']]
    gaps=[]
    for operation,sources in policy['required'].items():
        for source in sources:
            if source=='*':
                gaps.append({'kind':'required_source_set_not_finite','operation':operation})
                continue
            def matches(item):
                mapped=canonical_source(item.get('source') or source_for_url(item.get('source_url') or item.get('url') or ''))
                if mapped!=source:return False
                data=item.get('data') or {}
                if item.get('error') or isinstance(data,dict) and data.get('error'):return False
                if operation in ('search','resolve','import'):return item.get('kind')==operation
                if operation=='browser':return item.get('kind')=='browser.page' and not item.get('challenge') and not item.get('challenge_detected')
                return False
            success=any(matches(item) for item in observations)
            if operation=='download':
                success=any(a.get('status')=='verified' and source_for_url(a.get('source_url') or a.get('url') or '')==source
                            for a in store.artifacts(job_id) if a.get('revision')==job['revision'] and a.get('generation')==job['generation'])
            if not success:
                failures=[o for o in observations if o.get('kind')=='source_unavailable' and o.get('source')==source and o.get('operation')==operation]
                gaps.append({'kind':'required_source_operation_unavailable' if failures else 'required_source_operation_unobserved',
                             'source':source,'operation':operation,'code':failures[-1].get('code') if failures else None})
    return gaps
