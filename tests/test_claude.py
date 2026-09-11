import asyncio
from types import SimpleNamespace

import pytest

from ore.agent_sessions import AgentSession
from ore.claude import ClaudeBackend, ClaudeAuth, private_environment, usage_counts
from ore.store import Store
from ore.providers import BackendError


def fake_sdk():
    class Options(SimpleNamespace): pass
    class Client:
        def __init__(self, options):
            self.options = options; self.action = None; self.interrupted = asyncio.Event(); self.number = 0
        async def connect(self): pass
        async def disconnect(self): pass
        async def set_model(self, model): self.model = model
        async def query(self, prompt, session_id): self.ident = session_id; self.number += 1
        async def interrupt(self): self.interrupted.set()
        async def receive_response(self):
            if self.action: await self.action()
            Result = type('ResultMessage', (), {})
            message = Result(); message.usage = {'input_tokens': 10, 'output_tokens': 3, 'cache_read_input_tokens': 7}
            message.is_error = False; message.structured_output = None
            yield message
    return SimpleNamespace(ClaudeAgentOptions=Options, ClaudeSDKClient=Client,
        SdkMcpTool=lambda name, description, schema, handler: SimpleNamespace(name=name, handler=handler, input_schema=schema),
        HookMatcher=lambda **kw: SimpleNamespace(**kw), create_sdk_mcp_server=lambda **kw: kw,
        PermissionResultDeny=lambda **kw: kw)


def test_subscription_environment_does_not_inherit_api_billing(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-use')
    monkeypatch.setenv('CLAUDE_CODE_USE_BEDROCK', '1')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'must-not-copy')
    env = private_environment(tmp_path)
    assert 'ANTHROPIC_API_KEY' not in env and 'CLAUDE_CODE_USE_BEDROCK' not in env and 'CLAUDE_CODE_OAUTH_TOKEN' not in env
    assert env['CLAUDE_CONFIG_DIR'].startswith(str(tmp_path))
    assert (tmp_path / 'providers' / 'claude').stat().st_mode & 0o777 == 0o700


def test_cache_read_and_write_are_included_in_total_usage():
    assert usage_counts({'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 30})['totalTokens'] == 65
    assert usage_counts(None) == {}


@pytest.mark.asyncio
async def test_only_registered_tools_with_host_stamped_ids_dispatch(tmp_path):
    backend = ClaudeBackend(tmp_path, sdk=fake_sdk())
    calls = []
    async def handle(meta, args):
        calls.append((meta, args)); return {'success': True, 'contentItems': [{'type': 'inputText', 'text': 'ok'}]}
    ident = await backend.thread([{'name': 'fetch', 'inputSchema': {'type': 'object'}}], None, context_handler=handle)
    state = backend.sessions[ident]; state['running'] = True; backend.turn_ids[ident] = 'turn'
    options = state['client'].options
    assert options.tools == [] and options.strict_mcp_config is True and options.setting_sources == []
    assert options.skills == [] and options.plugins == []
    assert (await backend._tool(ident, 'fetch', {'_ore_dispatch_token': 'forged'}))['isError']
    denied = backend._pre_tool(ident, {'tool_name': 'Bash', 'tool_input': {}}, 'call')
    assert denied['hookSpecificOutput']['permissionDecision'] == 'deny'
    stamp = backend._pre_tool(ident, {'tool_name': 'mcp__ore__fetch', 'tool_input': {'url': 'https://example.com'}}, 'actual-provider-id')
    arguments = stamp['hookSpecificOutput']['updatedInput']
    assert (await backend._tool(ident, 'fetch', arguments))['isError'] is False
    assert calls[0][0]['callId'] == 'actual-provider-id' and calls[0][0]['threadId'] == ident
    assert calls[0][1] == {'url': 'https://example.com'}
    assert (await backend._tool(ident, 'fetch', arguments))['isError'] is True


@pytest.mark.asyncio
async def test_modified_arguments_cannot_use_an_existing_ticket(tmp_path):
    backend = ClaudeBackend(tmp_path, sdk=fake_sdk())
    async def forbidden(*args): raise AssertionError('dispatch must not run')
    ident = await backend.thread([{'name': 'fetch'}], None, context_handler=forbidden)
    backend.sessions[ident]['running'] = True; backend.turn_ids[ident] = 'turn'
    token = backend._pre_tool(ident, {'tool_name': 'mcp__ore__fetch', 'tool_input': {'url': 'good'}}, 'call')['hookSpecificOutput']['updatedInput']
    assert (await backend._tool(ident, 'fetch', {**token, 'url': 'bad'}))['isError']


@pytest.mark.asyncio
async def test_usage_is_cumulative_across_resumed_turns_and_keeps_watermark(tmp_path):
    backend = ClaudeBackend(tmp_path, sdk=fake_sdk()); usages = []
    ident = await backend.thread([], None, resume='known-session', usage_watermark={'totalTokens': 50}, on_usage=lambda value: usages.append(dict(value)))
    first = await backend.run(ident, 'one', effort='high')
    second = await backend.run(ident, 'two')
    assert ident == 'known-session' and first['cumulative_usage']['totalTokens'] == 70
    assert second['cumulative_usage']['totalTokens'] == 90 and len(usages) == 2
    assert backend.sessions[ident]['client'].options.effort == 'high'


@pytest.mark.asyncio
async def test_native_finish_confirms_provider_and_tool_settlement(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'state.db')); store.initialize()
    job = store.create_job({'goal': 'test'})
    backend = ClaudeBackend(tmp_path, sdk=fake_sdk())
    session = None
    async def handle(context, args):
        session.request_yield('finish')
        return {'accepted': True}
    session = AgentSession(backend=backend, store=store, job_id=job['id'], node_id='node', envelope_digest='scope',
        tool_specs=[{'name': 'finish', 'input_schema': {'type': 'object'}}], handler=handle, provider='claude_code')
    await session.start_or_resume(model='sonnet')
    state = backend.sessions[session.thread_id]
    async def action():
        wire = session.aliases['finish']
        stamp = backend._pre_tool(session.thread_id, {'tool_name': 'mcp__ore__' + wire, 'tool_input': {}}, 'finish-call')
        response = await backend._tool(session.thread_id, wire, stamp['hookSpecificOutput']['updatedInput'])
        assert response['isError'] is False
        await state['after_tool']()
        await state['client'].interrupted.wait()
    state['client'].action = action
    result = await asyncio.wait_for(session.run('finish', model='sonnet'), 3)
    assert result['settled'] and result['interruption']['acknowledged'] and result['control_yield'] == 'finish'
    await backend.close(); store.close()


@pytest.mark.asyncio
async def test_unknown_auth_and_login_are_not_fabricated(tmp_path):
    auth = ClaudeAuth(tmp_path)
    assert (await auth.login_status('missing'))['status'] == 'expired'
    assert (await auth.quota_status())['status'] == 'unknown'
    assert (await auth.cancel_login('missing'))['acknowledged']

@pytest.mark.asyncio
async def test_sdk_lifecycle_stays_on_one_owner_across_caller_tasks(tmp_path):
    sdk = fake_sdk()
    class OwnerClient(sdk.ClaudeSDKClient):
        async def connect(self): self.owner = asyncio.current_task()
        async def disconnect(self): assert self.owner is asyncio.current_task()
        async def query(self, prompt, session_id):
            assert self.owner is asyncio.current_task()
            await super().query(prompt, session_id)
    sdk.ClaudeSDKClient = OwnerClient
    backend = ClaudeBackend(tmp_path, sdk=sdk)
    ident = await backend.thread([], None)
    await asyncio.create_task(backend.run(ident, 'one'))
    await asyncio.create_task(backend.run(ident, 'two'))
    await backend.close()
    assert backend.sessions[ident]['provider_settled']


@pytest.mark.asyncio
@pytest.mark.parametrize('close_fails', [True, False])
async def test_timeout_ack_requires_confirmed_provider_shutdown(tmp_path, close_fails):
    sdk = fake_sdk()
    class StalledClient(sdk.ClaudeSDKClient):
        async def receive_response(self):
            await asyncio.Event().wait()
            yield None
        async def disconnect(self):
            if close_fails: raise RuntimeError('shutdown unconfirmed')
    sdk.ClaudeSDKClient = StalledClient
    backend = ClaudeBackend(tmp_path, sdk=sdk)
    ident = await backend.thread([], None)
    with pytest.raises(TimeoutError):
        await backend.run(ident, 'stall', timeout=.03)
    assert (await backend.interrupt(ident))['acknowledged'] is (not close_fails)
    if close_fails:
        with pytest.raises(BackendError, match='shutdown remains unconfirmed'):
            await backend.close()
    else:
        await backend.close()


@pytest.mark.asyncio
async def test_recreated_session_cannot_ack_an_unknown_previous_turn(tmp_path):
    backend = ClaudeBackend(tmp_path, sdk=fake_sdk())
    ident = await backend.thread([], None, resume='prior-provider-session')
    assert (await backend.interrupt(ident))['acknowledged'] is False
    await backend.close()
