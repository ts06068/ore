import asyncio

import pytest

from ore.claude import ClaudeBackend
from ore.providers import BackendError
from ore.workflow_native import claude_route
from test_claude import fake_sdk


@pytest.mark.parametrize(('provider', 'mission', 'model', 'effort'), [
    ({'model': 'opus', 'effort': 'low'}, {'model': 'sonnet', 'effort': 'high',
      'routing': {'model': 'custom', 'effort': 'medium'}}, 'opus', 'low'),
    ({'kind': 'claude_code'}, {'model': 'sonnet', 'effort': 'medium',
      'routing': {'model': 'custom', 'effort': 'high'}}, 'sonnet', 'medium'),
    ({'kind': 'claude_code'}, {'routing': {'model': 'custom', 'effort': 'low'}}, 'custom', 'low'),
    ({'kind': 'claude_code'}, {'model': 'opus'}, 'opus', 'high'),
    ({'kind': 'claude_code'}, {}, None, 'high'),
])
def test_claude_native_route_honors_approved_model_fields(provider, mission, model, effort):
    route = claude_route(provider, mission)
    assert route['model'] == model and route['effort'] == effort


@pytest.mark.asyncio
async def test_effort_change_reconnects_same_conversation_on_same_owner(tmp_path):
    sdk = fake_sdk()
    events = []

    class Client(sdk.ClaudeSDKClient):
        async def connect(self):
            events.append(('connect', asyncio.current_task(), self.options.resume,
                           self.options.session_id, self.options.effort))

        async def disconnect(self):
            events.append(('disconnect', asyncio.current_task()))

        async def query(self, prompt, session_id):
            events.append(('query', asyncio.current_task(), session_id, self.options.effort))
            await super().query(prompt, session_id)

    sdk.ClaudeSDKClient = Client
    backend = ClaudeBackend(tmp_path, sdk=sdk)
    ident = await backend.thread([], None)
    try:
        await asyncio.create_task(backend.run(ident, 'first', model='sonnet', effort='high'))
        await asyncio.create_task(backend.run(ident, 'second', model='sonnet', effort='low'))
        await asyncio.create_task(backend.run(ident, 'third', model='sonnet', effort='low'))
        assert [e[0] for e in events] == ['connect', 'query', 'disconnect', 'connect', 'query', 'query']
        assert events[0][2:] == (None, ident, 'high')
        assert events[3][2:] == (ident, None, 'low')
        assert [e[3] for e in events if e[0] == 'query'] == ['high', 'low', 'low']
        assert len({e[1] for e in events}) == 1
        assert backend.total_token_usage[ident]['totalTokens'] == 60
    finally:
        await backend.close()
    assert len({e[1] for e in events}) == 1


@pytest.mark.asyncio
async def test_close_cancels_stalled_owner_only_after_bounded_interrupt(tmp_path):
    sdk = fake_sdk()
    started = asyncio.Event()
    disconnected = asyncio.Event()

    class Client(sdk.ClaudeSDKClient):
        async def receive_response(self):
            started.set()
            await asyncio.Event().wait()
            yield None

        async def interrupt(self):
            await asyncio.Event().wait()

        async def disconnect(self):
            disconnected.set()

    sdk.ClaudeSDKClient = Client
    backend = ClaudeBackend(tmp_path, sdk=sdk)
    backend.shutdown_timeout = .03
    ident = await backend.thread([], None)
    running = asyncio.create_task(backend.run(ident, 'stall', timeout=100))
    await started.wait()
    receipt = await asyncio.wait_for(backend.interrupt(ident), .3)
    assert receipt['acknowledged'] is False
    assert receipt['reason'] == 'provider_interrupt_timeout'
    assert not disconnected.is_set()
    await asyncio.wait_for(backend.close(), .4)
    assert disconnected.is_set() and backend.sessions[ident]['owner'].done()
    assert (await backend.interrupt(ident))['acknowledged'] is True
    await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnect_timeout_is_not_acknowledged_and_close_raises(tmp_path):
    sdk = fake_sdk()

    class Client(sdk.ClaudeSDKClient):
        async def disconnect(self):
            await asyncio.Event().wait()

    sdk.ClaudeSDKClient = Client
    backend = ClaudeBackend(tmp_path, sdk=sdk)
    backend.shutdown_timeout = .03
    ident = await backend.thread([], None)
    await backend.run(ident, 'one')
    with pytest.raises(BackendError, match='shutdown remains unconfirmed'):
        await asyncio.wait_for(backend.close(), .4)
    assert backend.sessions[ident]['owner'].done()
    assert backend.sessions[ident]['cleanup_unconfirmed']
    assert (await backend.interrupt(ident))['acknowledged'] is False
    with pytest.raises(BackendError, match='shutdown is unconfirmed'):
        await backend.run(ident, 'must not restart')


@pytest.mark.asyncio
async def test_failed_effort_reconnect_does_not_submit_next_prompt(tmp_path):
    sdk = fake_sdk()

    class Client(sdk.ClaudeSDKClient):
        async def disconnect(self):
            raise RuntimeError('transport remains active')

    sdk.ClaudeSDKClient = Client
    backend = ClaudeBackend(tmp_path, sdk=sdk)
    ident = await backend.thread([], None)
    await backend.run(ident, 'first', effort='high')
    with pytest.raises(BackendError, match='shutdown remains unconfirmed'):
        await backend.run(ident, 'second', effort='low')
    state = backend.sessions[ident]
    assert state['client'].number == 1 and state['client'].options.effort == 'high'
    assert not state['running'] and state['cleanup_unconfirmed']
    with pytest.raises(BackendError, match='shutdown remains unconfirmed'):
        await backend.close()
