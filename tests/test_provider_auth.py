"""Official account protocol fixtures; no account, model or network calls."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from ore.codex import CodexBackend, BackendError
from ore.provider_auth import CodexAuth


class RPC:
    binary = '/fixture/codex'
    def __init__(self):
        self.calls = []
        self.listeners = set()
        self.account = {'account': None, 'requiresOpenaiAuth': True}
        self.login = {'type': 'chatgptDeviceCode', 'loginId': 'login-1',
                      'verificationUrl': 'https://auth.openai.com/codex/device', 'userCode': 'FIXTURE-CODE'}
        self.quota = {'accountId': 'private-fixture-id', 'ordinaryUsageAllowed': None,
                      'rateLimits': {'primary': {'usedPercent': 23, 'windowDurationMins': 300, 'resetsAt': 1000}}}
        self.cancel = {'status': 'canceled'}
    def add_account_listener(self, callback): self.listeners.add(callback)
    def remove_account_listener(self, callback): self.listeners.discard(callback)
    async def account_request(self, method, params=None):
        self.calls.append((method, params))
        result = {'account/read': self.account, 'account/login/start': self.login,
                  'account/login/cancel': self.cancel, 'account/rateLimits/read': self.quota}.get(method)
        if isinstance(result, BaseException): raise result
        return deepcopy(result)


async def test_auth_status_excludes_identity_and_uses_existing_account():
    rpc = RPC(); rpc.account['account'] = {'type': 'chatgpt', 'email': 'fixture-private@example.test', 'planType': 'plus'}
    auth = CodexAuth(rpc)
    status = await auth.auth_status()
    assert status['status'] == 'ready' and status['plan_type'] == 'plus'
    assert 'email' not in status and 'fixture-private' not in json.dumps(status)
    assert (await auth.start_login())['status'] == 'ready'
    assert not any(name == 'account/login/start' for name, _ in rpc.calls)


async def test_device_code_is_official_and_cancel_is_confirmed():
    rpc = RPC(); auth = CodexAuth(rpc)
    result = await auth.start_login()
    assert result['user_code'] == 'FIXTURE-CODE'
    assert rpc.calls[-1] == ('account/login/start', {'type': 'chatgptDeviceCode'})
    with pytest.raises(ValueError): await auth.start_login()
    assert not (await auth.cancel_login('another-login'))['acknowledged']
    assert (await auth.cancel_login('login-1')) == {'acknowledged': True, 'status': 'cancelled'}
    assert 'user_code' not in auth.login_status('login-1')
    await auth.close(); assert not rpc.listeners


async def test_cancel_not_found_does_not_claim_confirmed_stop():
    rpc = RPC(); auth = CodexAuth(rpc)
    await auth.start_login(); rpc.cancel = {'status': 'notFound'}
    assert not (await auth.cancel_login('login-1'))['acknowledged']
    with pytest.raises(ValueError): await auth.start_login()


@pytest.mark.parametrize('url', ['https://attacker.invalid/login', 'http://auth.openai.com/login', 'https://user:password@auth.openai.com/login'])
async def test_unapproved_verification_url_cancels_provider_flow(url):
    rpc = RPC(); rpc.login['verificationUrl'] = url
    with pytest.raises(ValueError): await CodexAuth(rpc).start_login()
    assert rpc.calls[-1] == ('account/login/cancel', {'loginId': 'login-1'})


async def test_login_notification_matches_owned_id_and_hides_raw_error():
    rpc = RPC(); auth = CodexAuth(rpc); events = []
    await auth.start_login(on_event=lambda event: events.append(event))
    auth._event('account/login/completed', {'loginId': 'another', 'success': True})
    assert events == []
    auth._event('account/login/completed', {'loginId': 'login-1', 'success': False, 'error': 'private-provider-string'})
    assert events == [{'login_id': 'login-1', 'status': 'failed'}]


async def test_quota_sparse_update_preserves_snapshot_and_never_infers_access():
    rpc = RPC(); auth = CodexAuth(rpc)
    first = await auth.quota_status()
    assert first['windows'][0]['remaining_percent'] == 77 and first['ordinary_usage_allowed'] is None
    assert 'private-fixture-id' not in json.dumps(first)
    auth._event('account/rateLimits/updated', {'rateLimits': {'primary': None, 'planType': None}})
    assert (await auth.quota_status())['windows'] == first['windows']
    auth._event('account/rateLimits/updated', {'rateLimits': {'primary': {'usedPercent': 0}}})
    updated = await auth.quota_status()
    assert updated['windows'][0]['used_percent'] == 0 and updated['ordinary_usage_allowed'] is None
    rpc.quota = RuntimeError('private-provider-error')
    assert (await auth.quota_status(refresh=True))['status'] == 'stale'
    auth._event('account/updated', {'authMode': None})
    assert (await auth.quota_status())['status'] == 'unsupported'


async def test_transport_never_relays_account_notifications_to_public_stream():
    backend = CodexBackend(binary='/fixture/codex'); public = []; private = []
    backend.on_event = lambda *value: public.append(value)
    backend.add_account_listener(lambda *value: private.append(value))
    stdout = asyncio.StreamReader()
    stdout.feed_data((json.dumps({'method': 'account/updated', 'params': {'authMode': 'chatgpt'}})+'\n').encode())
    stdout.feed_eof(); backend.process = SimpleNamespace(stdout=stdout)
    await backend._read()
    assert public == [] and private[0][0] == 'account/updated'


async def test_account_rpc_allowlist_rejects_token_injection_without_starting():
    backend = CodexBackend(binary='/fixture/codex'); backend.start = AsyncMock(); backend.request = AsyncMock(return_value={})
    for method, params in [('account/logout', {}), ('account/login/start', {'type': 'chatgptAuthTokens', 'accessToken': 'fixture-secret'})]:
        with pytest.raises(BackendError): await backend.account_request(method, params)
    backend.start.assert_not_called()
    await backend.account_request('account/read', {'refreshToken': False})
    backend.request.assert_awaited_once_with('account/read', {'refreshToken': False}, timeout=25)


async def test_sparse_other_limit_update_does_not_replace_default_bucket():
    backend=RPC()
    backend.quota={'rateLimits':{'limitId':'codex','primary':{'usedPercent':10}},
        'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':10}},'review':{'primary':{'usedPercent':20}}},
        'ordinaryUsageAllowed':True}
    auth=CodexAuth(backend)
    await auth.quota_status()
    auth._event('account/rateLimits/updated',{'rateLimits':{'limitId':'review','primary':{'usedPercent':40}}})
    assert auth._raw_quota['rateLimits']['primary']['usedPercent']==10
    result=await auth.quota_status()
    assert next(w for w in result['windows'] if w['id']=='codex:primary')['used_percent']==10
    assert next(w for w in result['windows'] if w['id']=='review:primary')['used_percent']==40
