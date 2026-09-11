from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore.claude import ClaudeAuth, ClaudeBackend, claude_quota_snapshot
from ore.connections import ConnectionManager
from test_claude import fake_sdk
from test_engine import engine as engine


@pytest.mark.parametrize(('status', 'allowed'), [('allowed', True), ('allowed_warning', True),
                                                ('rejected', False), ('unrecognized', None)])
def test_observed_subscription_limits_use_shared_public_schema(status, allowed):
    value = claude_quota_snapshot({'status': status, 'utilization': .75, 'resets_at': 2000,
                                  'observed_at': 1000, 'rate_limit_type': 'five_hour',
                                  'overage_status': 'allowed', 'raw': {'secret': 'private'}}, now=1001)
    assert value['status'] == 'available' and value['ordinary_usage_allowed'] is allowed
    assert value['windows'] == [{'id': 'claude_code:five_hour', 'used_percent': 75.0,
                                 'remaining_percent': 25.0, 'resets_at': 2000}]
    assert not value['stale'] and 'raw' not in value and 'overage_status' not in value


@pytest.mark.parametrize('utilization', [None, '0.2', True, -1, 2, float('nan'), float('inf')])
def test_missing_or_invalid_utilization_does_not_invent_allowance(utilization):
    value = claude_quota_snapshot({'utilization': utilization, 'resetsAt': 2000}, now=1000)
    assert value['ordinary_usage_allowed'] is None
    assert value['windows'][0]['used_percent'] is None
    assert value['windows'][0]['remaining_percent'] is None


def test_unobserved_quota_is_unknown_and_old_observation_is_stale():
    assert claude_quota_snapshot({}) == {'status': 'unknown', 'windows': [],
                                       'ordinary_usage_allowed': None, 'stale': False}
    assert claude_quota_snapshot({'status': 'allowed', 'observed_at': 1000}, now=1301)['stale']
    assert claude_quota_snapshot({'status': 'rejected', 'observed_at': 1000,
                                 'resets_at': 1005}, now=1006)['status'] == 'stale'


@pytest.mark.asyncio
async def test_sdk_rate_event_remains_visible_in_provider_status(engine):
    sdk = fake_sdk()

    class Client(sdk.ClaudeSDKClient):
        async def receive_response(self):
            Event = type('RateLimitEvent', (), {})
            event = Event()
            event.rate_limit_info = SimpleNamespace(status='rejected', utilization=.96,
                resets_at=4_000_000_000, rate_limit_type='seven_day', overage_status='allowed',
                raw={'secret': 'must-not-expose'})
            yield event
            async for result in super().receive_response():
                yield result

    sdk.ClaudeSDKClient = Client
    auth = ClaudeAuth(engine.settings.state_dir)
    auth.auth_status = AsyncMock(return_value={'status': 'ready'})
    backend = ClaudeBackend(engine.settings.state_dir, sdk=sdk, auth=auth)
    ident = await backend.thread([], None)
    try:
        await backend.run(ident, 'fake provider event')
        engine.provider_auth = {'claude_code': auth}
        response = await ConnectionManager(engine, None).providers_status()
        public = next(x for x in response['providers'] if x['provider'] == 'claude_code')
        assert public['quota']['status'] == 'available'
        assert public['quota']['ordinary_usage_allowed'] is False
        assert public['quota']['windows'][0]['used_percent'] == 96
        assert public['quota']['windows'][0]['remaining_percent'] == 4
        assert public['quota']['windows'][0]['resets_at'] == 4_000_000_000
        assert 'overage_status' not in str(public) and 'must-not-expose' not in str(public)
    finally:
        await backend.close()
