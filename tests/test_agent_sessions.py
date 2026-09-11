import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore.agent_sessions import AgentSession, SessionMismatch, ToolCallContext, ToolResult, tool_aliases
from ore.codex import CodexBackend, DISABLED_FEATURES, scoped_native_config
from ore.policy import AccessDenied


class MemoryStore:
    def __init__(self): self.rows = {}
    def get_document(self, collection, key): return copy.deepcopy(self.rows.get((collection, json.dumps(key))))
    def put_document(self, collection, key, value, **kwargs):
        old = self.get_document(collection, key) or {}
        assert kwargs.get('expected_version', old.get('state_version', 0)) == old.get('state_version', 0)
        result = {**copy.deepcopy(value), 'state_version': old.get('state_version', 0) + 1}
        self.rows[collection, json.dumps(key)] = result
        return copy.deepcopy(result)


class NativeBackend:
    def __init__(self):
        self.options = {}; self.resume = None; self.turn_ids = {}; self.total_token_usage = {}
        self.interrupted = asyncio.Event(); self.action = None; self.interrupt_count = 0
        self.settle = {'acknowledged': True, 'pending': 0}
    async def thread(self, tools, handler, **kwargs):
        self.tools, self.options, self.resume = tools, kwargs, kwargs.get('resume')
        return self.resume or 'thread-1'
    async def call(self, name, args=None, call_id='call-1'):
        result = await self.options['context_handler']({'threadId': 'thread-1', 'turnId': 'turn-1',
            'callId': call_id, 'tool': name}, args or {})
        await self.options['after_tool']()
        return result
    async def run(self, tid, prompt, **kwargs):
        self.turn_ids[tid] = 'turn-1'
        if self.action: await self.action()
        return {'turn': {'id': 'turn-1', 'status': 'completed'}, 'text': 'done'}
    async def interrupt(self, tid):
        self.interrupt_count += 1; self.interrupted.set()
        return {'requested': True, 'acknowledged': True, 'thread_id': tid, 'turn_id': 'turn-1'}
    async def settle_tools(self, tid, **kwargs): return dict(self.settle)


def session(store=None, backend=None, handler=None, **kwargs):
    return AgentSession(backend=backend or NativeBackend(), store=store or MemoryStore(),
        job_id='job', node_id='node', envelope_digest='approved-v1',
        tool_specs=[{'name': 'fetch', 'description': 'read', 'input_schema': {'type': 'object'}}],
        handler=handler or AsyncMock(return_value={'ok': True}), **kwargs)


def data(result): return json.loads(result['contentItems'][0]['text'])


def test_aliases_collision_free_stable_and_bounded():
    specs = [{'name': 'a.b'}, {'name': 'a_b'}, {'name': 'a-b'}, {'name': 'x' * 160}]
    aliases = tool_aliases(specs)
    assert len(set(aliases.values())) == 4
    assert aliases == tool_aliases(list(reversed(specs)))
    assert all(len(name) <= 64 and name.replace('_', '').isalnum() for name in aliases.values())
    with pytest.raises(ValueError): tool_aliases([{'name': 'same'}, {'name': 'same'}])


def test_native_config_cannot_enable_shell_or_network_bypasses(tmp_path, monkeypatch):
    (tmp_path/'config.toml').write_text('[mcp_servers.user_secret]\ncommand="must-not-launch"\n')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    config = scoped_native_config({'features.shell_tool': True, 'web_search': 'live', 'sandbox_mode': 'danger-full-access'})
    assert config['features.code_mode'] is True and config['features.code_mode_host'] is True
    assert all(config['features.' + name] is False for name in DISABLED_FEATURES if name not in ('code_mode', 'code_mode_host'))
    assert config['web_search'] == 'disabled' and config['sandbox_mode'] == 'read-only'
    assert config['mcp_servers.user_secret.enabled'] is False


async def test_native_dispatch_identity_scope_and_input_validation():
    calls = []
    async def handler(ctx, args): calls.append((ctx, args)); return {'ok': True}
    value = session(handler=handler); await value.start_or_resume(model='model')
    async def action():
        allowed = await value.backend.call(value.aliases['fetch'])
        denied = await value.backend.call('shell', call_id='bad')
        assert data(allowed) == {'ok': True}
        assert data(denied)['code'] == 'capability_not_allowed'
    value.backend.action = action
    result = await value.run('goal')
    assert result['settled'] is True and len(calls) == 1
    ctx = calls[0][0]
    assert isinstance(ctx, ToolCallContext) and ctx.name == 'fetch' and ctx.call_id == 'call-1'
    assert ctx.operation_id == ToolCallContext('codex', 'thread-1', 'thread-1', 'turn-1', 'call-1', 'fetch', 'alias').operation_id
    assert value.backend.options['scoped_native'] is True


async def test_resume_preserves_identity_usage_and_rejects_changed_envelope():
    store = MemoryStore(); first = session(store=store)
    await first.start_or_resume(model='m')
    await first._usage({'totalTokens': 30, 'inputTokens': 20})
    second = session(store=store)
    assert second.usage_baseline == {'totalTokens': 30, 'inputTokens': 20}
    await second.start_or_resume(model='m')
    assert second.backend.resume == 'thread-1'
    assert second.backend.options['usage_watermark']['totalTokens'] == 30
    second.envelope_digest = 'different'
    with pytest.raises(SessionMismatch): await second.start_or_resume(model='m')


async def test_repeated_provider_identity_changed_arguments_never_dispatches():
    calls = []
    async def handler(ctx, args): calls.append(args); return {'ok': True}
    value = session(handler=handler); await value.start_or_resume()
    async def action():
        await value.backend.call(value.aliases['fetch'], {'x': 1})
        result = await value.backend.call(value.aliases['fetch'], {'x': 2})
        assert data(result)['code'] == 'SessionMismatch'
    value.backend.action = action
    with pytest.raises(SessionMismatch): await value.run('goal')
    assert calls == [{'x': 1}]


async def test_yield_sends_receipt_then_stops_new_calls_and_confirms_settlement():
    results = []; value = None
    async def handler(ctx, args):
        value.request_yield('delegate')
        return {'accepted': True}
    value = session(handler=handler); await value.start_or_resume()
    async def action():
        results.append(await value.backend.call(value.aliases['fetch']))
        results.append(await value.backend.call(value.aliases['fetch'], call_id='late'))
    value.backend.action = action
    result = await value.run('delegate')
    assert data(results[0]) == {'accepted': True}
    assert data(results[1])['code'] == 'agent_yielding'
    assert result['control_yield'] == 'delegate' and result['settled'] is True
    assert value.backend.interrupt_count == 1


async def test_unconfirmed_tool_ack_prevents_resuming():
    store = MemoryStore(); value = session(store=store); await value.start_or_resume()
    value.backend.settle = {'acknowledged': False, 'pending': 1}
    receipt = await value.interrupt()
    assert receipt['acknowledged'] is False
    with pytest.raises(SessionMismatch): await session(store=store).start_or_resume()


async def test_usage_monotonic_and_failed_budget_hook_stops_dispatch():
    seen = []
    async def account(usage):
        seen.append(usage)
        if usage['totalTokens'] >= 100: raise AccessDenied('budget')
    value = session(on_usage=account); await value.start_or_resume()
    await value._usage({'totalTokens': 50})
    await value._usage({'totalTokens': 30})
    assert seen[-1]['totalTokens'] == 50 and seen[-1]['counter_regressed'] == ['totalTokens']
    with pytest.raises(AccessDenied): await value._usage({'totalTokens': 110})
    assert value._yield == 'budget' and not value._accepting
    assert seen[-1]['provider'] == 'codex' and seen[-1]['session_id'] == 'thread-1'


async def test_parallel_dispatch_bound_and_per_call_images():
    active = 0; peak = 0
    async def handler(ctx, args):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
        return ToolResult({'id': ctx.call_id}, [f'data:image/png;base64,{ctx.call_id}'])
    value = session(handler=handler, max_parallel=2); await value.start_or_resume()
    results = []
    async def action():
        results.extend(await asyncio.gather(*(value.backend.call(value.aliases['fetch'], call_id=str(i)) for i in range(7))))
    value.backend.action = action
    await value.run('parallel')
    assert peak == 2
    for i, result in enumerate(results):
        assert result['contentItems'][1]['imageUrl'] == f'data:image/png;base64,{i}'
    with pytest.raises(AccessDenied): ToolResult({}, ['https://external.test/private.png']).wire()


async def test_transport_uses_provider_call_id_not_rpc_id_and_denies_permissions():
    backend = CodexBackend(binary='/unused'); backend.send = AsyncMock()
    backend.handlers['thread'] = AsyncMock(side_effect=AssertionError('legacy bypass'))
    received = []
    async def handler(context, args): received.append(context); return {'ok': True}
    backend._context_handlers['thread'] = handler
    params = {'threadId': 'thread', 'turnId': 'turn', 'callId': 'stable', 'tool': 'native', 'arguments': {}}
    await backend._server_request({'id': 999, 'method': 'item/tool/call', 'params': params})
    assert received[0]['callId'] == 'stable' and received[0]['turnId'] == 'turn'
    await backend._server_request({'id': 1000, 'method': 'item/tool/call', 'params': {k:v for k,v in params.items() if k != 'callId'}})
    assert len(received) == 1 and backend.send.call_args.args[0]['result']['success'] is False
    await backend._server_request({'id': 1001, 'method': 'item/permissions/requestApproval', 'params': {}})
    assert backend.send.call_args.args[0]['result']['permissions'] == {}


async def test_transport_usage_updates_are_cumulative_ordered_and_nonblocking():
    backend = CodexBackend(binary='/unused'); seen = []
    async def usage(total):
        await asyncio.sleep(.001); seen.append(total)
    backend._usage_handlers['thread'] = usage
    backend._record_usage('thread', {'totalTokens': 100, 'inputTokens': 60})
    backend._record_usage('thread', {'totalTokens': 150, 'inputTokens': 90})
    backend._record_usage('thread', {'totalTokens': 120, 'inputTokens': 80})
    await backend.flush_usage('thread')
    assert [x['totalTokens'] for x in seen] == [100, 150, 150]
    assert seen[-1]['counter_regressed'] == ['totalTokens', 'inputTokens']


def test_scoped_native_uses_dedicated_process_and_leaves_legacy_configuration():
    backend = CodexBackend(binary='/unused')
    native = backend.scoped_native_backend()
    assert native is backend.scoped_native_backend() and native is not backend
    assert native.scoped_native_backend() is native
    assert backend.config['features.code_mode_host'] is False
    assert native.config['features.code_mode_host'] is True
    assert all(native.config['features.' + name] is False for name in DISABLED_FEATURES if name not in ('code_mode', 'code_mode_host'))


async def test_usage_baseline_does_not_swallow_first_model_tokens():
    value = session(); await value.start_or_resume()
    await value._usage({'totalTokens': 900})
    assert value.usage_baseline == {}
    assert value.cumulative_usage == {'totalTokens': 900}
    resumed = session(store=value.store)
    assert resumed.usage_baseline == {'totalTokens': 900}


async def test_stop_cancels_and_settles_active_callback_before_ack():
    entered = asyncio.Event(); cleanup = asyncio.Event()
    async def handler(ctx, args):
        entered.set()
        try: await asyncio.Event().wait()
        finally: cleanup.set()
    value = session(handler=handler); await value.start_or_resume()
    value._accepting = True
    task = asyncio.create_task(value.backend.call(value.aliases['fetch']))
    await entered.wait()
    receipt = await value.interrupt()
    await asyncio.gather(task, return_exceptions=True)
    assert receipt['acknowledged'] is True and cleanup.is_set()
    assert receipt['tool_ack']['adapter_pending'] == 0


async def test_orphan_running_session_requires_provider_settlement_before_resume():
    store = MemoryStore(); first = session(store=store); await first.start_or_resume()
    first._persist(status='running', last_turn_id='turn-before-crash')
    second = session(store=store)
    await second.start_or_resume()
    assert second.backend.interrupt_count == 1
    assert second._state['status'] == 'ready'


async def test_fenced_owner_only_appends_late_telemetry_never_overwrites_state():
    from ore.store import LeaseLost
    class FencedStore(MemoryStore):
        expired = False
        def put_document(self, collection, key, value, **kwargs):
            if self.expired and kwargs.get('lease'):
                raise LeaseLost('retired worker')
            return super().put_document(collection, key, value, **kwargs)
    store = FencedStore(); seen = []
    value = session(store=store, lease={'task_id': 'task', 'worker_id': 'worker', 'fence': 1, 'revision': 1}, on_usage=seen.append)
    await value.start_or_resume(); value._persist(status='running', last_turn_id='turn-1')
    before = store.get_document('workflow.agent_session', value.session_key)
    store.expired = True
    await value._usage({'totalTokens': 500})
    receipt = await value.interrupt()
    assert receipt['acknowledged'] is True and seen[-1]['totalTokens'] == 500
    assert store.get_document('workflow.agent_session', value.session_key) == before
    rows = [row for (collection, _), row in store.rows.items() if collection == 'workflow.agent_observation']
    assert {row['kind'] for row in rows} == {'usage', 'interruption'}
    assert all(row['owner']['fence'] == 1 and row['thread_id'] == 'thread-1' for row in rows)
    with pytest.raises(LeaseLost): value._persist(status='completed')


async def test_failed_turn_observer_keeps_rpc_reader_alive_for_stop_receipt():
    from ore.store import LeaseLost
    backend = CodexBackend(binary='/unused')
    reader = asyncio.StreamReader(); backend.process = SimpleNamespace(stdout=reader)
    backend.interrupt = AsyncMock(return_value={'acknowledged': True})
    def stopped(turn_id): raise LeaseLost('already fenced')
    backend._turn_handlers['thread'] = stopped
    future = asyncio.get_running_loop().create_future(); backend.turns['thread'] = future
    for method, turn in [('turn/started', {'id': 'turn', 'status': 'inProgress'}), ('turn/completed', {'id': 'turn', 'status': 'interrupted'})]:
        reader.feed_data((json.dumps({'method': method, 'params': {'threadId': 'thread', 'turn': turn}}) + '\n').encode())
    reader.feed_eof(); await backend._read(); await asyncio.sleep(0)
    assert future.result()['turn']['status'] == 'interrupted'
    assert backend.turn_completions['thread', 'turn'].is_set()
    assert isinstance(backend._usage_errors['thread'], LeaseLost)


async def test_concurrent_native_start_launches_one_process_and_one_reader(monkeypatch):
    process = SimpleNamespace(returncode=None, wait=AsyncMock(), terminate=lambda:None, kill=lambda:None)
    async def spawn(*args, **kwargs):
        await asyncio.sleep(.01)
        return process
    create = AsyncMock(side_effect=spawn)
    monkeypatch.setattr('ore.codex.asyncio.create_subprocess_exec', create)
    backend = CodexBackend(binary='/unused').scoped_native_backend()
    backend._read = AsyncMock(); backend._drain_stderr = AsyncMock()
    backend.request = AsyncMock(return_value={}); backend.send = AsyncMock()
    results = await asyncio.gather(*(backend.start() for _ in range(5)))
    assert all(value is backend for value in results)
    assert create.await_count == 1 and backend._read.await_count == 1 and backend._drain_stderr.await_count == 1
    assert backend.request.await_count == 1
    await backend.close()
