import json

import httpx
import pytest

from ore.credentials import CredentialPool, CredentialTransport, CredentialUnavailable
from ore.policy import RateLimiter
from ore.store import Store


@pytest.fixture
def pool(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'state.db')); store.initialize()
    profile = {'id': 'one', 'sources': {'scopus': {'credential_pool': [
        {'id': 'a', 'api_key_ref': 'a'}, {'id': 'b', 'api_key_ref': 'b'}]}}}
    secrets = {'a': 'secret-a', 'b': 'secret-b'}
    clock = [100.0]
    value = CredentialPool(store, profile, secrets.get, clock=lambda: clock[0])
    yield value, profile, secrets, clock
    store.close()


def test_shared_quota_blocks_rotation_and_survives_restart(pool):
    p, profile, secrets, clock = pool
    entry = p.select('scopus')
    p.observe('scopus', entry, 429, {'X-ELS-Status': 'QUOTA_EXCEEDED', 'X-RateLimit-Reset': '200'})
    again = CredentialPool(p.store, profile, secrets.get, clock=lambda: clock[0])
    with pytest.raises(CredentialUnavailable) as exc: again.select('scopus')
    assert exc.value.resets_at == 200
    clock[0] = 201
    assert again.select('scopus')['id'] == 'a'


def test_unauthorized_alias_does_not_create_an_independent_budget(pool):
    p, profile, secrets, clock = pool
    profile['sources']['scopus']['credential_pool'][1]['quota_group'] = 'pretend-independent'
    p.observe('scopus', p.select('scopus'), 200, {'X-RateLimit-Remaining': '0'})
    with pytest.raises(CredentialUnavailable): p.select('scopus', exclude=['a'])


def test_late_response_cannot_reopen_exhausted_group(pool):
    p, *_ = pool
    entry = p.select('scopus')
    p.observe('scopus', entry, 429, {'X-ELS-Status': 'QUOTA_EXCEEDED', 'X-RateLimit-Reset': '200'})
    p.observe('scopus', entry, 200, {'X-RateLimit-Remaining': '99'})
    with pytest.raises(CredentialUnavailable): p.select('scopus')


def test_revoked_key_alias_cannot_be_reused(pool):
    p, profile, secrets, _ = pool
    secrets['b'] = secrets['a']
    p.observe('scopus', p.select('scopus'), 401, {})
    with pytest.raises(CredentialUnavailable): p.select('scopus')
    public = json.dumps(p.public_status())
    assert 'secret-a' not in public and 'api_key_ref' not in public


def test_ncbi_does_not_admit_multiple_active_keys(pool):
    p, profile, *_ = pool
    profile['sources']['pubmed'] = profile['sources']['scopus']
    with pytest.raises(ValueError, match='one active'): p.entries('pubmed')


@pytest.mark.asyncio
async def test_authorized_independent_quota_switch_retries_get_once(pool):
    p, profile, secrets, _ = pool
    profile['sources']['scopus']['credential_pool'][1].update(independent_quota=True, quota_group='licensed-second')
    calls = []
    async def serve(request):
        calls.append(request.headers['X-ELS-APIKey'])
        return httpx.Response(429, headers={'X-ELS-Status': 'QUOTA_EXCEEDED'}) if len(calls) == 1 else httpx.Response(200, json={'ok': True})
    transport = CredentialTransport(httpx.MockTransport(serve), p, 'scopus', RateLimiter(), interval=0)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get('https://api.elsevier.com/content/search/scopus')
    assert response.status_code == 200 and calls == ['secret-a', 'secret-b']


@pytest.mark.asyncio
async def test_throttle_never_rotates_even_independent_credentials(pool):
    p, profile, *_ = pool
    profile['sources']['scopus']['credential_pool'][1].update(independent_quota=True, quota_group='licensed-second')
    calls = []
    async def serve(request):
        calls.append(request); return httpx.Response(429, headers={'Retry-After': '3'})
    async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(serve), p, 'scopus', RateLimiter(), interval=0)) as client:
        response = await client.get('https://api.elsevier.com/content/search/scopus')
    assert response.status_code == 429 and len(calls) == 1


@pytest.mark.asyncio
async def test_credentials_never_sent_to_arbitrary_origin(pool):
    p, *_ = pool
    async def forbidden(request): raise AssertionError('network was reached')
    async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(forbidden), p, 'scopus', RateLimiter(), interval=0)) as client:
        with pytest.raises(CredentialUnavailable, match='destination'):
            await client.get('https://example.com/collect')


def test_same_key_cannot_escape_quota_through_an_independent_group_alias(pool):
    p, profile, secrets, _ = pool
    secrets['b'] = secrets['a']
    profile['sources']['scopus']['credential_pool'][1].update(independent_quota=True, quota_group='alias')
    p.observe('scopus', p.select('scopus'), 429, {'X-RateLimit-Reset': '200'})
    with pytest.raises(CredentialUnavailable): p.select('scopus', exclude=['a'])


def test_late_alias_response_does_not_reopen_identical_key(pool):
    p, profile, secrets, _ = pool
    secrets['b'] = secrets['a']
    profile['sources']['scopus']['credential_pool'][1].update(independent_quota=True, quota_group='alias')
    first = p.select('scopus'); concurrent_alias = p.select('scopus', exclude=['a'])
    p.observe('scopus', first, 429, {'X-RateLimit-Reset': '200'})
    p.observe('scopus', concurrent_alias, 200, {'X-RateLimit-Remaining': '99'})
    with pytest.raises(CredentialUnavailable): p.select('scopus')
    assert all(row['status'] == 'rate_limited' for row in p.public_status('scopus') if row['operation'] == 'search')
