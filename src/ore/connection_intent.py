"""Small, explicit connection commands that do not require a working model."""
from __future__ import annotations
import re

PROVIDER_PATTERNS = {
    'codex': r'\bcodex\b|코덱스|\bchatgpt\b|챗지피티',
    'claude_code': r'\bclaude(?:[ -]code)?\b|클로드',
    'openai': r'\bopenai\b|오픈에이아이',
    'anthropic': r'\banthropic\b|앤트로픽',
    'scopus': r'\bscopus\b|\belsevier\b|스코퍼스|엘스비어',
    'pubmed': r'\bpubmed\b|\bncbi\b|펍메드|퍼브메드',
    'crossref': r'\bcross[ -]?ref\b|크로스레프',
    'wos': r'\bclarivate\b|\bwos\b|\bweb of science\b|클래리베이트',
}
SETUP = re.compile(r'\b(?:connect|login|log[ -]?in|sign[ -]?in|setup|set up|register|enroll|authenticate)\b|'
    r'\b(?:create|request|issue|obtain|get|generate|add|save|enter|apply for|open)\b.{0,45}\b(?:account|api[ -]?key|access[ -]?key|token)\b|'
    r'로그인|연결|설정|가입|발급|신청|등록|입력', re.I | re.S)
COLLECTION = re.compile(r'\b(?:collect\w*|retriev\w*|download\w*|scrap(?:e|ing))\b|\bfind\b.{0,80}\b(?:articles?|papers?|studies)\b|'
    r'\bsearch for\b|수집|검색해|검색하|찾아|다운로드', re.I | re.S)
SENSITIVE = re.compile(r'\b(?:password|passwd|pw|api[ _-]?key|access[ _-]?token)\s*[:=]|비밀번호\s*[:=]|\bsk-[A-Za-z0-9_-]{12,}', re.I)
ENROLL = re.compile(r'\b(?:register|enroll|create|request|issue|obtain|get|generate)\b|가입|발급|신청', re.I)


def _provider_pattern(pattern):
    return '|'.join(part.replace(r'\b', r'(?<![a-z0-9_])', 1).replace(r'\b', r'(?![a-z0-9_])', 1) for part in pattern.split('|'))


def connection_setup_intent(content):
    if not isinstance(content, str) or len(content) > 2000 or not SETUP.search(content) or COLLECTION.search(content):
        return None
    mentions = sorted((match.start(), match.end(), name, match.group())
        for name, pattern in PROVIDER_PATTERNS.items()
        for match in re.finditer(_provider_pattern(pattern), content, re.I))
    if not mentions:
        return None
    providers = []
    for index, (start, end, name, alias) in enumerate(mentions):
        # Bind API only to this alias. A conjunction/comma or another provider
        # cannot turn a separate subscription login into API billing.
        tail = content[end:mentions[index + 1][0] if index + 1 < len(mentions) else len(content)]
        api = re.match(r'\s*(?:의\s*)?(?:[-:(]\s*)?api(?![a-z0-9_])', tail, re.I)
        if api and re.fullmatch(r'chatgpt|챗지피티', alias, re.I):
            name = 'openai'
        elif api and name == 'claude_code':
            name = 'anthropic'
        providers.append(name)
    return {'providers': list(dict.fromkeys(providers)),
            'action': 'request_agent' if ENROLL.search(content) else 'connect',
            'contains_credentials': bool(SENSITIVE.search(content))}


def credential_fields(provider, kind='source'):
    if kind == 'model':
        return ['api_key'] if provider in {'openai', 'anthropic'} else []
    from .source_policy import _registry
    entry = _registry().get(provider, {})
    fields = list(dict.fromkeys([*entry.get('credentials', {}), *entry.get('optional_credentials', {})]))
    if provider in {'scopus', 'wos', 'wos_expanded', 'pubmed', 'scienceon', 'dbpia'}:
        fields = ['username', 'password', *fields]
    return list(dict.fromkeys(fields))


def has_configured_api_key(engine, provider, profile):
    """Check usable protected references or the adapter's configured environment."""
    import os
    from .source_policy import _registry
    config = profile.get('sources', {}).get(provider, {})
    pool = config.get('credential_pool')
    if pool is not None:
        refs = [item.get('api_key_ref') for item in pool
                if isinstance(item, dict) and item.get('enabled') is not False]
    elif config.get('api_key_ref'):
        refs = [config['api_key_ref']]
    else:
        entry = _registry().get(provider, {})
        env = config.get('api_key_env') or {**entry.get('credentials', {}), **entry.get('optional_credentials', {})}.get('api_key')
        return bool(env and os.environ.get(env))
    return any(isinstance(ref, str) and ref and engine.secrets.get(ref) for ref in refs)


async def connect_from_chat(manager, conversation, intent):
    """Connect cards without changing a running mission, model or access profile."""
    engine = manager.engine
    connections = engine.connections
    profile = manager._request_profile(conversation)['id']
    backend = conversation['settings'].get('backend', {}).get('kind', 'codex')
    backend = 'claude_code' if backend in {'claude', 'claude-code'} else backend
    model = conversation['settings'].get('model')
    cards, notes = [], []
    for provider in intent['providers']:
        card = connections.create({'provider': provider, 'conversation_id': conversation['id'], 'access_profile_ref': profile})
        try:
            if card['kind'] == 'model':
                action = 'start_login' if provider in {'codex', 'claude_code'} else 'refresh'
            else:
                action = 'refresh'
            if card['status'] not in {'awaiting_auth', 'connecting', 'cancel_unconfirmed'}:
                import uuid
                card = await connections.action(card['id'], {'action': action, 'expected_version': card['state_version'],
                    'idempotency_key': 'chat-' + uuid.uuid4().hex})
            source_profile = engine.profile({'access_profile_ref': card['access_profile_ref']})
            configured_key = has_configured_api_key(engine, provider, source_profile)
            if configured_key and card['kind'] == 'source':
                notes.append(provider + ': an API key is already configured. Verify it in the card and select its profile for collection.')
            if (card['kind'] == 'source' and not configured_key and intent['action'] == 'request_agent'
                    and provider != 'crossref' and card['status'] not in {'ready', 'approval_pending'}):
                import uuid
                card = await connections.action(card['id'], {'action': 'request_agent', 'expected_version': card['state_version'],
                    'idempotency_key': 'chat-' + uuid.uuid4().hex, 'agent_backend': backend, 'agent_model': model})
        except (ValueError, KeyError, PermissionError):
            card = connections.public(connections._read(card['id']))
            notes.append(provider + ': complete the next step in its connection card.')
        cards.append(card)
        if provider == 'crossref':
            notes.append('Crossref public metadata search needs no account or API key. A contact email is optional.')
        elif provider == 'pubmed':
            notes.append('PubMed search works without a key at the public rate. NCBI permits one active key per account; a replacement invalidates the old key.')
        elif card['status'] == 'approval_pending':
            notes.append(provider + ': approval is pending; collection will exclude this unavailable operation until it is ready.')
        elif card['kind'] == 'model' and provider in {'openai', 'anthropic'}:
            notes.append(provider + ': enter your API key in the protected card and select this connection explicitly to use API billing.')
        elif card['kind'] == 'model':
            notes.append(provider + ': ' + ('your existing login is connected.' if card['status'] == 'ready' else 'continue the official sign-in in the connection card.'))
    return cards, notes
