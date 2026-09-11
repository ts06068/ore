"""Explicit provider setup, scoped access checks and approval waiting states."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from fastapi import APIRouter, Request
from .handoffs import safe_checkpoint
from .source_policy import canonical_source, canonical_operation, source_catalog, scholarly_failure, source_for_url

PROVIDERS = {
    'pubmed': 'https://www.ncbi.nlm.nih.gov/account/settings/',
    'scopus': 'https://dev.elsevier.com/apikey/manage',
    'wos': 'https://developer.clarivate.com/apis/wos-starter',
    'wos_expanded': 'https://developer.clarivate.com/apis/wos',
    'scienceon': 'https://scienceon.kisti.re.kr/apigateway/apiMain.do',
    'kci': 'https://www.kci.go.kr/kciportal/main.kci',
    'dbpia': 'https://www.dbpia.co.kr/'}


async def setup_source(engine, source, body):
    from .operator_access import operator_mission
    source = canonical_source(source)
    operation = canonical_operation(body.get('operation', 'search'))
    conversation_id = body.get('conversation_id')
    if conversation_id and engine.store.get_document('conversation', conversation_id) is None:
        raise KeyError('Unknown conversation')
    profile = engine.profile({'access_profile_ref': body.get('access_profile_ref', 'public')})
    catalog = {item['id']: item for item in source_catalog(profile, engine.secrets.get)}
    if source not in catalog:
        raise ValueError('Unknown or unavailable source adapter')
    entry = catalog[source]
    url = PROVIDERS.get(source) or safe_checkpoint(entry.get('documentation_url') or entry.get('docs_url') or entry.get('docs'))
    if not url:
        raise ValueError('This source has no registered setup address')
    if profile['id'] == 'public':
        profile = {**profile, 'id': 'connection-'+source, 'name': source+' connection', 'persist_session': True}
        engine.save_profile(profile)
    record = engine.store.get_job(body['job_id']) if body.get('job_id') else None
    if body.get('job_id') and record is None:
        raise KeyError(body['job_id'])
    if record and engine.profile(record['mission'])['id'] != profile['id']:
        raise ValueError('Select the same access profile as the mission')
    if not record:
        mission = operator_mission(url, profile=profile['id'],
            goal='Connect '+source+' using an existing provider account', conversation_id=conversation_id)
        mission['connection_id'] = body.get('connection_id')
        record = engine.create(mission, queued=False)
    handoff = engine.handoffs.create(record['id'], 'source_setup',
        'Use your existing provider account first. Complete credentials, MFA and terms in the protected connection controls.',
        source=source, operation=operation, context={'url': url})
    handoff = engine.handoffs.update(handoff['id'], {'provider_url': url, 'checkpoint_url': safe_checkpoint(url),
        'conversation_id': conversation_id, 'connection_id': body.get('connection_id'), 'setup_steps': [
        'Use an existing account first', 'Review the selected API and account requirements',
        'Complete authentication, MFA and terms when requested',
        'Store credentials through protected fields', 'Verify the operation; exclude pending API approvals'],
        'credential_fields': ['username', 'password', 'api_key'] if source in PROVIDERS else []})
    await engine.pause(record['id'], 'awaiting_user')
    manager = getattr(engine, 'connections', None)
    if manager and not body.get('connection_id'):
        manager.link_source_handoff(handoff, conversation_id=conversation_id)
        handoff = engine.handoffs.get(handoff['id'])
    return engine.handoffs.public(handoff)


def create_onboarding_router(engine, browser):
    router = APIRouter(prefix='/v1/sources', tags=['onboarding'])

    @router.post('/{source}/setup')
    async def setup(source: str, request: Request):
        return await setup_source(engine, source, await request.json())

    @router.post('/{source}/check')
    async def check(source: str, request: Request):
        return await check_source(engine, browser, source, await request.json())
    return router


def _check_identity(engine, source, profile):
    """Bind a bounded check to its policy and actual protected credentials."""
    import os
    from .models import canonical_digest
    from .source_policy import _registry
    config = profile.get('sources', {}).get(source, {})
    refs = {value for name, value in config.items() if name.endswith('_ref') and isinstance(value, str)}
    refs.update(item['api_key_ref'] for item in config.get('credential_pool', [])
                if isinstance(item, dict) and item.get('api_key_ref'))
    entry = _registry().get(source, {})
    env = {**entry.get('credentials', {}), **entry.get('optional_credentials', {})}
    values = {ref: engine.secrets.get(ref) for ref in refs}
    values.update({'env:'+name: os.environ.get(config.get(name+'_env', default)) for name, default in env.items()})
    return canonical_digest({'profile': {k: v for k, v in profile.items() if k != 'source_readiness'}, 'credentials': values})


async def check_source(engine, browser, source, body):
    source = canonical_source(source)
    operation = canonical_operation(body.get('operation', 'search'))
    profile = engine.profile({'access_profile_ref': body.get('access_profile_ref','public')})
    if source not in {x['id'] for x in source_catalog(profile,engine.secrets.get)}:raise ValueError('Unknown source adapter')
    checked_identity = _check_identity(engine, source, profile)
    now = datetime.now(timezone.utc); evidence_id = uuid.uuid4().hex
    state, failure_code, scope = 'ready', None, operation+'_bounded_check'
    try:
        if operation == 'browser':
            sid=body.get('session_id')
            if not sid or not browser.exists(sid):raise ValueError('A live browser session is required')
            session=await browser.summary(sid)
            if not safe_checkpoint(session.get('url')) or source_for_url(session['url'],profile)!=source:raise ValueError('Browser is not on the selected provider')
            record=engine.store.get_job(session['job_id'])
            if engine.profile(record['mission'])['id']!=profile['id']:raise ValueError('Browser profile does not match')
            observation=await browser.command(sid,'observe')
            if observation.get('challenge_detected'):raise ValueError('The browser still displays a challenge')
            scope='browser_navigation_only_not_subscription_entitlement'
        elif operation in ('search','resolve'):
            if browser.remote:
                await browser.remote.source_check(profile,source,operation,query=body.get('query') or 'cardiology',identifier=body.get('identifier'))
                scope='executor_'+operation+'_bounded_check'
            else:
                from ore_scholarly import search, resolve
                import httpx
                from .policy import AccessPolicy, GuardedTransport, RateLimiter
                # Verification is an explicit operator request. It tests exactly this
                # operation even when its previous readiness was pending or unknown.
                check_profile={**profile,'source_readiness':{}}
                mission={'sources':[source],'source_policy':{'allow':{operation:[source]}}}
                transport=GuardedTransport(AccessPolicy(mission,check_profile,operation=operation,source_hint=source),RateLimiter(engine.store),profile_id=profile['id'])
                config={**profile.get('sources',{}).get(source,{}),'secret_resolver':engine.secrets.get}
                async with httpx.AsyncClient(transport=transport,timeout=20,follow_redirects=True) as client:
                    config['client']=client
                    if operation=='search':await asyncio.wait_for(search(source,body.get('query') or 'cardiology',limit=1,config=config),25)
                    else:
                        if not body.get('identifier'):raise ValueError('An identifier is required for a resolve check')
                        await asyncio.wait_for(resolve(source,body['identifier'],config=config),25)
        else:raise ValueError('Verify download/import operations through a scoped mission audit')
    except Exception as exc:
        failure=scholarly_failure(exc,source,operation)
        state=failure.get('state') or failure.get('code','temporarily_unavailable')
        if state not in {'unconfigured','auth_required','approval_pending','entitlement_denied','rate_limited','unsupported'}:state='temporarily_unavailable'
        failure_code=failure.get('code',type(exc).__name__)
    current = engine.profile({'access_profile_ref': profile['id']})
    changed = _check_identity(engine, source, current) != checked_identity
    if changed:
        state, failure_code = 'unknown', 'verification_configuration_changed'
    evidence={'id':evidence_id,'source':source,'operation':operation,'access_profile_ref':profile['id'],'state':state,
        'observed_at':now.isoformat(),'scope':scope,'failure_code':failure_code}
    engine.store.put_document('source_check',evidence_id,evidence,expected_version=0)
    readiness={**evidence,'evidence_ref':'source_check:'+evidence_id,'expires_at':(now+timedelta(hours=24)).isoformat()}
    prior_readiness = profile.get('source_readiness', {}).get(source, {}).get(operation)
    current_readiness = current.get('source_readiness', {}).get(source, {}).get(operation)
    # A concurrent completed check or credential write owns its newer readiness.
    if current_readiness == prior_readiness:
        current.setdefault('source_readiness',{}).setdefault(source,{})[operation]=readiness
        engine.save_profile(current)
    return readiness
