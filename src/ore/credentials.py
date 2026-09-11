"""Operator-configured credentials share durable provider quota groups.

Rotation is bounded to existing authorized credentials. The default group is
provider-wide; an operator must explicitly configure independently authorized
quota groups before those groups can be used independently.
"""
from __future__ import annotations

from copy import deepcopy
from email.utils import parsedate_to_datetime
import hashlib
import math
import time

import httpx


class CredentialUnavailable(RuntimeError):
    def __init__(self, source, status, resets_at=None):
        super().__init__(f'{source}: {status}')
        self.code, self.source, self.resets_at = status, source, resets_at


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def retry_time(value, now):
    delay = _number(value)
    if delay is not None: return now + delay
    try:
        return max(now, parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


class CredentialPool:
    def __init__(self, store, profile, secret_resolver, *, clock=time.time):
        self.store, self.profile, self.secret_resolver, self.clock = store, profile, secret_resolver, clock

    def entries(self, source):
        config = self.profile.get('sources', {}).get(source, {})
        rows = config.get('credential_pool')
        if rows is None:
            rows = [{'id': 'default', 'api_key_ref': config['api_key_ref']}] if config.get('api_key_ref') else []
        if not isinstance(rows, list) or len(rows) > 20:
            raise ValueError('credential_pool must contain at most 20 configured credentials')
        result, seen = [], set()
        for raw in rows:
            if not isinstance(raw, dict): raise ValueError('Credential entry must be an object')
            if raw.get('enabled') is False: continue
            ident, ref = raw.get('id'), raw.get('api_key_ref')
            if not isinstance(ident, str) or not ident or ident in seen or not isinstance(ref, str) or not ref:
                raise ValueError('Credential entries need distinct IDs and protected key references')
            seen.add(ident)
            group = raw.get('quota_group') if raw.get('independent_quota') is True else None
            if group is not None and (not isinstance(group, str) or not group.strip()):
                raise ValueError('Independent credentials need an explicit quota group')
            result.append({'id': ident, 'ref': ref, 'quota_group': group or 'shared:' + source})
        if source == 'pubmed' and len(result) > 1:
            raise ValueError('NCBI permits one active key per account; configure one active credential')
        return result

    def _identity(self, source, entry):
        value = self.secret_resolver(entry['ref'])
        if not isinstance(value, str) or not value: return None, None
        # Same key referenced under different profile/entry names shares state.
        identity = hashlib.sha256((source + '\0' + value).encode()).hexdigest()
        return identity, value

    def _group_key(self, source, entry, operation):
        return [source, entry['quota_group'], operation]

    def select(self, source, operation='search', *, exclude=()):
        now = self.clock(); blocked = []
        for entry in self.entries(source):
            if entry['id'] in exclude: continue
            identity, secret = self._identity(source, entry)
            if not identity: continue
            credential = self.store.get_document('credential.health', identity) or {}
            if credential.get('status') in ('invalid', 'revoked'): continue
            state = self.store.get_document('credential.quota', self._group_key(source, entry, operation)) or {}
            key_state = self.store.get_document('credential.key_quota', [source, identity, operation]) or {}
            if key_state.get('status') in ('rate_limited', 'quota_exhausted') and (key_state.get('resets_at') is None or key_state['resets_at'] > now):
                blocked.append(key_state); continue
            until = state.get('resets_at')
            if state.get('status') in ('rate_limited', 'quota_exhausted') and (until is None or until > now):
                blocked.append(state); continue
            return {**entry, 'identity': identity, 'secret': secret}
        if blocked:
            resets = [r['resets_at'] for r in blocked if r.get('resets_at') is not None]
            raise CredentialUnavailable(source, 'rate_limited', min(resets) if resets else None)
        raise CredentialUnavailable(source, 'credentials_missing')

    def observe(self, source, entry, status, headers, operation='search'):
        now = self.clock(); headers = httpx.Headers(headers)
        key = self._group_key(source, entry, operation)
        remaining, limit = _number(headers.get('x-ratelimit-remaining')), _number(headers.get('x-ratelimit-limit'))
        reset = _number(headers.get('x-ratelimit-reset'))
        retry = retry_time(headers.get('retry-after'), now)
        with self.store._tx() as conn:
            row = self.store._doc(conn, 'credential.quota', key)
            old = deepcopy(row['data']) if row else {}
            state = {**old, 'source': source, 'quota_group': entry['quota_group'], 'operation': operation,
                     'observed_at': now, 'scope': 'provider_quota_group', 'status': 'ready'}
            if remaining is not None: state['remaining'] = int(remaining)
            if limit is not None: state['limit'] = int(limit)
            if reset is not None: state['resets_at'] = reset
            if status == 429:
                state['status'] = 'quota_exhausted' if 'QUOTA_EXCEEDED' in headers.get('x-els-status', '').upper() else 'rate_limited'
                state['resets_at'] = reset or retry or now + 30
            elif remaining == 0:
                state['status'] = 'quota_exhausted'
            elif old.get('status') in ('rate_limited', 'quota_exhausted') and (old.get('resets_at') or float('inf')) > now:
                # A late concurrent response cannot reopen a closed group.
                state['status'] = old['status']; state['resets_at'] = old.get('resets_at')
                if old.get('remaining') == 0: state['remaining'] = 0
            self.store._put(conn, 'credential.quota', key, state)
            identity_key = [source, entry['identity'], operation]
            prior_key = self.store._doc(conn, 'credential.key_quota', identity_key)
            prior_key = prior_key['data'] if prior_key else {}
            key_state = dict(state)
            if (prior_key.get('status') in ('rate_limited', 'quota_exhausted')
                    and (prior_key.get('resets_at') or float('inf')) > now
                    and state['status'] == 'ready'):
                key_state.update(status=prior_key['status'], resets_at=prior_key.get('resets_at'))
                if prior_key.get('remaining') == 0: key_state['remaining'] = 0
            self.store._put(conn, 'credential.key_quota', identity_key, key_state)
            if status == 401:
                self.store._put(conn, 'credential.health', entry['identity'], {'status': 'invalid', 'observed_at': now})
        return state

    def public_status(self, source=None):
        sources = [source] if source else list(self.profile.get('sources', {}))
        rows = []
        for name in sources:
            for entry in self.entries(name):
                identity, _ = self._identity(name, entry)
                health = self.store.get_document('credential.health', identity) if identity else None
                for operation in ('search', 'resolve'):
                    state = self.store.get_document('credential.quota', self._group_key(name, entry, operation)) or {}
                    key_state = self.store.get_document('credential.key_quota', [name, identity, operation]) or {}
                    if key_state.get('status') in ('rate_limited', 'quota_exhausted') and (key_state.get('resets_at') or float('inf')) > self.clock():
                        state = key_state
                    rows.append({'source': name, 'credential_id': entry['id'], 'quota_group': entry['quota_group'],
                        'operation': operation, 'status': (health or {}).get('status') or state.get('status', 'unknown') if identity else 'unconfigured',
                        **{field: state.get(field) for field in ('remaining', 'limit', 'resets_at', 'observed_at')},
                        'scope': 'provider_quota_group'})
            if not self.entries(name):
                state = self.store.get_document('credential.quota', [name, 'shared:' + name, 'search']) or {}
                rows.append({'source': name, 'credential_id': None, 'quota_group': 'shared:' + name,
                    'operation': 'search', 'status': state.get('status', 'unknown'),
                    **{field: state.get(field) for field in ('remaining', 'limit', 'resets_at', 'observed_at')},
                    'scope': 'provider_quota_group'})
        return rows


CREDENTIAL_HOSTS = {'scopus': {'api.elsevier.com'}, 'pubmed': {'eutils.ncbi.nlm.nih.gov'},
    'wos': {'api.clarivate.com', 'wos-api.clarivate.com'}, 'wos_expanded': {'api.clarivate.com', 'wos-api.clarivate.com'},
    'crossref': {'api.crossref.org'}, 'dbpia': {'api.dbpia.co.kr'}}


class CredentialTransport(httpx.AsyncBaseTransport):
    """Capture real quota headers and retry GETs only with admitted credentials."""
    def __init__(self, inner, pool, source, limiter, operation='search', interval=1):
        self.inner, self.pool, self.source, self.limiter = inner, pool, source, limiter
        self.operation, self.interval = operation, interval

    async def handle_async_request(self, request):
        if request.method != 'GET' or request.url.host not in CREDENTIAL_HOSTS.get(self.source, set()):
            raise CredentialUnavailable(self.source, 'credential_destination_denied')
        attempted = []
        while True:
            entry = self.pool.select(self.source, self.operation, exclude=attempted)
            attempted.append(entry['id'])
            headers = httpx.Headers(request.headers); url = request.url
            if self.source == 'pubmed': url = url.copy_merge_params({'api_key': entry['secret']})
            elif self.source == 'scopus': headers['X-ELS-APIKey'] = entry['secret']
            elif self.source in ('wos', 'wos_expanded'): headers['X-ApiKey'] = entry['secret']
            elif self.source == 'crossref': headers['Crossref-Plus-API-Token'] = 'Bearer ' + entry['secret']
            elif self.source == 'dbpia': url = url.copy_merge_params({'key': entry['secret']})
            await self.limiter.acquire(f'credential:{self.source}:{entry["identity"]}', self.interval)
            # Group pacing crosses independent workers/profiles, even when keys differ.
            await self.limiter.acquire(f'quota:{self.source}:{entry["quota_group"]}:{self.operation}', self.interval)
            response = await self.inner.handle_async_request(httpx.Request('GET', url, headers=headers, extensions=request.extensions))
            state = self.pool.observe(self.source, entry, response.status_code, response.headers, self.operation)
            switch = response.status_code == 401 or (response.status_code == 429 and state['status'] == 'quota_exhausted')
            if switch:
                try: self.pool.select(self.source, self.operation, exclude=attempted)
                except CredentialUnavailable: return response
                await response.aclose()
                continue
            return response

    async def aclose(self):
        await self.inner.aclose()


class QuotaObservingTransport(httpx.AsyncBaseTransport):
    """Observe quota for public or legacy environment-based credentials."""
    def __init__(self, inner, pool, source, operation='search'):
        self.inner, self.pool, self.source, self.operation = inner, pool, source, operation

    async def handle_async_request(self, request):
        state = self.pool.store.get_document('credential.quota', [self.source, 'shared:' + self.source, self.operation]) or {}
        if state.get('status') in ('rate_limited', 'quota_exhausted') and (state.get('resets_at') is None or state['resets_at'] > self.pool.clock()):
            raise CredentialUnavailable(self.source, 'rate_limited', state.get('resets_at'))
        response = await self.inner.handle_async_request(request)
        entry = {'quota_group': 'shared:' + self.source, 'identity': 'public:' + self.source}
        self.pool.observe(self.source, entry, response.status_code, response.headers, self.operation)
        return response

    async def aclose(self):
        await self.inner.aclose()
