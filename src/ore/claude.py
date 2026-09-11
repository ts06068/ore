"""Official Claude Code SDK transport with ORE-scoped tools and private login."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from urllib.parse import urlsplit

from .codex import BackendError
from .models import canonical_digest


def sdk_module():
    try:
        import claude_agent_sdk
        return claude_agent_sdk
    except ImportError:
        raise BackendError('Install ore-engine[claude] to use Claude Code') from None


def claude_binary():
    configured = os.environ.get('ORE_CLAUDE_BIN') or shutil.which('claude')
    if configured:
        return configured
    try:
        bundled = Path(sdk_module().__file__).parent / '_bundled' / ('claude.exe' if os.name == 'nt' else 'claude')
        return str(bundled) if bundled.is_file() else None
    except BackendError:
        return None


def private_environment(state_dir):
    root = Path(os.environ.get('ORE_CLAUDE_CONFIG_DIR', str(Path(state_dir) / 'providers' / 'claude')))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    env = dict(os.environ)
    # A subscription selection never silently charges an inherited API/cloud key.
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                'ANTHROPIC_BASE_URL', 'CLAUDECODE'):
        env.pop(key, None)
    env['CLAUDE_CONFIG_DIR'] = str(root)
    return env


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def usage_counts(raw):
    raw = raw or {}
    if not any(k in raw for k in ('input_tokens', 'output_tokens')):
        return {}
    count = lambda key: max(0, int(raw.get(key, 0) or 0))
    cached, written = count('cache_read_input_tokens'), count('cache_creation_input_tokens')
    inputs, outputs = count('input_tokens') + cached + written, count('output_tokens')
    return {'inputTokens': inputs, 'outputTokens': outputs, 'cachedInputTokens': cached,
            'cacheWriteInputTokens': written, 'totalTokens': inputs + outputs}


def claude_quota_snapshot(raw, *, now=None):
    """Project only provider-observed subscription limits into the shared UI schema."""
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get('status')
    allowed = True if status in ('allowed', 'allowed_warning') else False if status == 'rejected' else None
    utilization = raw.get('utilization')
    used = utilization * 100 if type(utilization) in (int, float) and 0 <= utilization <= 1 else None
    reset = raw.get('resets_at') if raw.get('resets_at') is not None else raw.get('resetsAt')
    reset = reset if type(reset) in (int, float) and 0 < reset < float('inf') else None
    observed = raw.get('observed_at')
    observed = observed if type(observed) in (int, float) and 0 < observed < float('inf') else None
    now = time.time() if now is None else now
    stale = observed is not None and (now - observed > 300 or (reset is not None and now >= reset))
    window_type = raw.get('rate_limit_type')
    window = {'id': ('claude_code:' + window_type[:100]) if isinstance(window_type, str) and window_type else 'claude_code:observed',
              'used_percent': used, 'remaining_percent': 100 - used if used is not None else None}
    if reset is not None:
        window['resets_at'] = reset
    windows = [window] if used is not None or reset is not None or isinstance(window_type, str) else []
    result = {'status': 'stale' if stale else 'available' if windows or allowed is not None else 'unknown',
              'windows': windows, 'ordinary_usage_allowed': allowed, 'stale': stale}
    if observed is not None:
        result['observed_at'] = observed
    return result


class ClaudeAuth:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.logins = {}
        self._auth_cache = None
        self.quota = {'status': 'unknown', 'provider': 'claude_code', 'reason': 'provider_has_not_reported_limits'}

    async def auth_status(self):
        if self._auth_cache and time.time() - self._auth_cache.get('observed_at', 0) < 30:
            return dict(self._auth_cache)
        binary = claude_binary()
        if not binary:
            return {'status': 'unavailable', 'installed': False, 'message_code': 'claude_sdk_missing'}
        process = await asyncio.create_subprocess_exec(binary, 'auth', 'status', env=private_environment(self.state_dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), 15)
            data = json.loads(raw)
        except (TimeoutError, ValueError):
            if process.returncode is None:
                process.kill(); await process.wait()
            return {'status': 'unknown', 'installed': True, 'message_code': 'auth_status_unavailable'}
        self._auth_cache = {'status': 'ready' if process.returncode == 0 and data.get('loggedIn') else 'auth_required',
                'installed': True, 'mode': data.get('authMethod'), 'plan_type': data.get('subscriptionType'),
                'observed_at': time.time()}
        return dict(self._auth_cache)

    async def start_login(self, on_event=None):
        if (await self.auth_status()).get('status') == 'ready': return {'status': 'ready'}
        for item in self.logins.values():
            if item['process'].returncode is None:
                return dict(item['public'])
        binary = claude_binary()
        if not binary:
            raise BackendError('Install ore-engine[claude] before signing in')
        ident = str(uuid.uuid4())
        env = private_environment(self.state_dir)
        # The connection card owns navigation; the CLI still owns OAuth tokens.
        env['BROWSER'] = 'true'
        process = await asyncio.create_subprocess_exec(binary, 'auth', 'login', '--claudeai', env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        item = {'process': process, 'public': {'login_id': ident, 'status': 'awaiting_auth',
                'expires_at': time.time() + 600, 'login_code_required': True}, 'callback': on_event, 'seen': asyncio.Event()}
        self.logins[ident] = item
        item['reader'] = asyncio.create_task(self._read_login(ident))
        try:
            await asyncio.wait_for(item['seen'].wait(), 5)
        except TimeoutError:
            pass
        return dict(item['public'])

    async def _read_login(self, ident):
        item = self.logins[ident]
        try:
            async with asyncio.timeout(600):
                async for raw in item['process'].stdout:
                    line = raw.decode(errors='replace')
                    for value in re.findall(r'https://[^\s\x1b]+', line):
                        url = value.rstrip(').,')
                        host = (urlsplit(url).hostname or '').lower()
                        if host in ('claude.ai', 'console.anthropic.com', 'platform.claude.com', 'auth.anthropic.com'):
                            item['public']['verification_url'] = url
                            item['seen'].set()
                    # Raw CLI output, authorization codes and tokens are never logged.
                code = await item['process'].wait()
                if item['public']['status'] not in ('cancelled', 'expired'):
                    item['public']['status'] = 'ready' if code == 0 else 'failed'
        except TimeoutError:
            await self.cancel_login(ident)
            item['public']['status'] = 'expired'
        finally:
            self._auth_cache = None
            item['seen'].set()
            if item['callback']:
                await maybe_await(item['callback']({'login_id': ident, 'status': item['public']['status']}))

    async def submit_login(self, login_id, code):
        item = self.logins.get(login_id)
        if not item or item['process'].returncode is not None:
            raise BackendError('Login is no longer active')
        if not isinstance(code, str) or not code.strip() or len(code) > 8192 or '\n' in code or '\r' in code:
            raise ValueError('Invalid login completion code')
        item['process'].stdin.write((code.strip() + '\n').encode())
        await item['process'].stdin.drain()
        return {'status': 'awaiting_auth'}

    async def cancel_login(self, login_id):
        item = self.logins.get(login_id)
        if not item:
            return {'status': 'cancelled', 'acknowledged': True}
        process = item['process']
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill(); await process.wait()
        item['public']['status'] = 'cancelled'
        return {'status': 'cancelled', 'acknowledged': process.returncode is not None}

    async def quota_status(self):
        return claude_quota_snapshot(self.quota)

    async def login_status(self, login_id):
        item = self.logins.get(login_id)
        return dict(item['public']) if item else {'login_id': login_id, 'status': 'expired'}

    async def close(self):
        for ident in list(self.logins):
            await self.cancel_login(ident)


class ClaudeBackend:
    """Implements the AgentSession transport using the official SDK only.

    The trusted PreToolUse hook stamps a one-use dispatch ticket containing the
    real provider tool-use ID. A model cannot mint a receipt identity or call the
    in-process tool server without a matching hook ticket.
    """
    def __init__(self, state_dir, *, sdk=None, auth=None):
        self.state_dir = Path(state_dir)
        self.sdk = sdk
        self.auth = auth or ClaudeAuth(state_dir)
        self.sessions = {}
        self.turn_ids = {}
        self.turn_completions = {}
        self.total_token_usage = {}
        self.resumed_turn_status = {}
        self.shutdown_timeout = 10

    async def thread(self, tools, handler, *, model=None, instructions='', resume=None,
                     context_handler=None, on_usage=None, after_tool=None, usage_watermark=None, on_turn=None, **kwargs):
        sdk = self.sdk or sdk_module()
        ident = resume or str(uuid.uuid4())
        if ident in self.sessions:
            return ident
        work = self.state_dir / 'providers' / 'claude-work'
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {'tools': {t['name']: t for t in tools}, 'handler': context_handler,
                 'usage_hook': on_usage, 'after_tool': after_tool, 'on_turn': on_turn,
                 'tickets': {}, 'active': set(), 'usage_ids': set(), 'running': False,
                 'done': asyncio.Event(), 'model': model, 'connected': False,
                 'provider_settled': not bool(resume), 'queue': asyncio.Queue()}
        self.sessions[ident] = state
        self.total_token_usage[ident] = dict(usage_watermark or {})
        definitions = []
        for spec in tools:
            name = spec['name']
            schema = copy.deepcopy(spec.get('inputSchema', {'type': 'object'}))
            schema.setdefault('properties', {})['_ore_dispatch_token'] = {'type': 'string', 'description': 'Internal transport field; omit.'}
            async def invoke(arguments, name=name):
                return await self._tool(ident, name, arguments)
            definitions.append(sdk.SdkMcpTool(name, spec.get('description', name), schema, invoke))
        async def pre_tool(data, tool_use_id, context):
            return self._pre_tool(ident, data, tool_use_id)
        async def after_tool(data, tool_use_id, context):
            if state['after_tool']:
                await maybe_await(state['after_tool']())
            return {}
        async def deny_unknown(name, arguments, context):
            return sdk.PermissionResultDeny(message='Only ORE-scoped tools are available')
        env = private_environment(self.state_dir)
        # SDK env merges with os.environ; explicitly clear inherited billing settings.
        for key in set(os.environ) - set(env):
            env[key] = ''
        options = sdk.ClaudeAgentOptions(tools=[], allowed_tools=[f'mcp__ore__{n}' for n in state['tools']],
            mcp_servers={'ore': sdk.create_sdk_mcp_server(name='ore', tools=definitions)}, strict_mcp_config=True,
            permission_mode='default', can_use_tool=deny_unknown, setting_sources=[], skills=[], plugins=[],
            hooks={'PreToolUse': [sdk.HookMatcher(hooks=[pre_tool])], 'PostToolUse': [sdk.HookMatcher(hooks=[after_tool])]}, cwd=str(work), env=env,
            model=model, system_prompt=instructions, resume=resume, session_id=None if resume else ident,
            include_partial_messages=False, stderr=lambda line: None,
            settings=json.dumps({'disableAllHooks': False, 'enableAllProjectMcpServers': False}),
            cli_path=claude_binary() if self.sdk is None else None)
        state['client'] = sdk.ClaudeSDKClient(options=options)
        return ident

    def _pre_tool(self, ident, data, call_id):
        state = self.sessions[ident]
        wire = str(data.get('tool_name', ''))
        name = wire.removeprefix('mcp__ore__')
        args = dict(data.get('tool_input', {}))
        args.pop('_ore_dispatch_token', None)
        if not state['running'] or not wire.startswith('mcp__ore__') or name not in state['tools'] or not call_id:
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                    'permissionDecisionReason': 'Missing scoped ORE call identity'}}
        token = uuid.uuid4().hex
        state['tickets'][token] = {'name': name, 'digest': canonical_digest(args), 'call_id': call_id,
                                   'turn_id': self.turn_ids[ident]}
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                      'updatedInput': {**args, '_ore_dispatch_token': token}}}

    async def _tool(self, ident, name, arguments):
        state = self.sessions[ident]
        args = dict(arguments)
        ticket = state['tickets'].pop(args.pop('_ore_dispatch_token', None), None)
        if not ticket or ticket['name'] != name or ticket['digest'] != canonical_digest(args) or not state['running']:
            return {'content': [{'type': 'text', 'text': 'Missing or invalid ORE dispatch ticket'}], 'isError': True}
        task = asyncio.current_task(); state['active'].add(task)
        try:
            result = await state['handler']({'tool': name, 'threadId': ident, 'turnId': ticket['turn_id'], 'callId': ticket['call_id']}, args)
            content = []
            for block in result.get('contentItems', []):
                if block.get('type') == 'inputText': content.append({'type': 'text', 'text': block['text']})
                elif block.get('type') == 'inputImage':
                    prefix, data = block['imageUrl'].split(',', 1)
                    content.append({'type': 'image', 'data': data, 'mimeType': prefix[5:].split(';')[0]})
            return {'content': content, 'isError': result.get('success') is False}
        finally:
            state['active'].discard(task)

    async def run(self, ident, prompt, *, model=None, effort=None, timeout=600, **kwargs):
        state = self.sessions[ident]
        if state['running']: raise BackendError('Claude session already has an active turn')
        if state.get('cleanup_unconfirmed'): raise BackendError('Previous Claude process shutdown is unconfirmed')
        if kwargs.get('images'): raise BackendError('Images must be provided through scoped tool results')
        # SDK connect/disconnect enter an AnyIO cancel scope. Keep both on one
        # owner task even when successive planner turns use different tasks.
        state['prior_turn_settled'] = state['provider_settled']
        state['running'] = True
        state['provider_settled'] = False
        state['done'] = asyncio.Event()
        if state.get('owner') is None or state['owner'].done():
            state['owner'] = asyncio.create_task(self._owner(ident))
        future = asyncio.get_running_loop().create_future()
        await state['queue'].put((future, prompt, model, effort, timeout))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await self.interrupt(ident)
            future.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)
            raise

    async def _owner(self, ident):
        state = self.sessions[ident]
        try:
            while request := await state['queue'].get():
                future, prompt, model, effort, timeout = request
                try:
                    result = await self._run_turn(ident, prompt, model=model, effort=effort, timeout=timeout)
                except BaseException as exc:
                    if not future.done(): future.set_exception(exc)
                    if isinstance(exc, asyncio.CancelledError): raise
                else:
                    if not future.done(): future.set_result(result)
        finally:
            await self._disconnect(ident)

    async def _disconnect(self, ident):
        state = self.sessions[ident]
        if state['connected']:
            try:
                # Keep SDK cancel-scope cleanup on its owner; wait_for would
                # create another task and violate the AnyIO scope ownership.
                async with asyncio.timeout(self.shutdown_timeout):
                    await state['client'].disconnect()
            except asyncio.CancelledError:
                state['provider_settled'] = False
                state['cleanup_unconfirmed'] = True
                raise
            except Exception:
                state['provider_settled'] = False
                state['cleanup_unconfirmed'] = True
                return False
            state['connected'] = False
            state['cleanup_unconfirmed'] = False
            state['provider_settled'] = True
        return state['provider_settled']

    async def _run_turn(self, ident, prompt, *, model=None, effort=None, timeout=600):
        state = self.sessions[ident]
        client = state['client']
        turn = str(uuid.uuid4())
        self.turn_ids[ident] = turn
        self.turn_completions[(ident, turn)] = state['done']
        base = dict(self.total_token_usage[ident]); local = {}; text = ''; result_seen = False
        try:
            async with asyncio.timeout(timeout):
                if effort:
                    if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
                        raise BackendError('Unsupported Claude reasoning effort')
                    if state['connected'] and effort != getattr(client.options, 'effort', None):
                        if not state.get('prior_turn_settled'):
                            raise BackendError('Claude effort cannot change before the prior turn settles')
                        if not await self._disconnect(ident):
                            raise BackendError('Claude process shutdown remains unconfirmed')
                        # The SDK has no effort setter. Reconnect to the same
                        # provider conversation with a changed startup option.
                        client.options.resume = ident
                        client.options.session_id = None
                    client.options.effort = effort
                if not state['connected']:
                    # Partial starts also require owner-task cleanup.
                    state['connected'] = True
                    state['provider_settled'] = False
                    await client.connect()
                if model and hasattr(client, 'set_model'): await client.set_model(model)
                if state['on_turn']: await maybe_await(state['on_turn'](turn))
                if state.get('interrupted'):
                    state['provider_settled'] = True
                    return {'text': '', 'turn': {'id': turn, 'status': 'interrupted'},
                            'cumulative_usage': dict(self.total_token_usage[ident])}
                await client.query(prompt, session_id=ident)
                async for message in client.receive_response():
                    kind = type(message).__name__
                    if kind == 'SystemMessage' and getattr(message, 'subtype', '') == 'init':
                        actual = message.data.get('session_id')
                        if actual and actual != ident:
                            raise BackendError('Claude changed the requested session identity')
                    if kind == 'AssistantMessage':
                        counts = usage_counts(getattr(message, 'usage', None))
                        mid = getattr(message, 'message_id', None) or getattr(message, 'uuid', None)
                        if counts and mid and mid not in state['usage_ids']:
                            state['usage_ids'].add(mid)
                            local = {k: local.get(k, 0) + v for k, v in counts.items()}
                            await self._usage(ident, {k: base.get(k, 0) + v for k, v in local.items()})
                        for block in message.content:
                            if type(block).__name__ == 'TextBlock': text += block.text
                    elif kind == 'ResultMessage':
                        result_seen = True
                        state['provider_settled'] = True
                        structured = getattr(message, 'structured_output', None)
                        if structured is not None:
                            text = json.dumps(structured, ensure_ascii=False)
                        counts = usage_counts(getattr(message, 'usage', None))
                        if counts:
                            await self._usage(ident, {k: base.get(k, 0) + max(v, local.get(k, 0)) for k, v in counts.items()})
                        if getattr(message, 'is_error', False) and not state.get('interrupted'):
                            raise BackendError('Claude provider turn failed')
                    elif getattr(message, 'type', '') == 'rate_limit_event' or kind == 'RateLimitEvent':
                        value = getattr(message, 'rate_limit_info', {})
                        if not isinstance(value, dict) and hasattr(value, '__dict__'): value = vars(value)
                        if isinstance(value, dict):
                            self.auth.quota = {'provider': 'claude_code', 'observed_at': time.time(),
                                **{k: value[k] for k in ('status', 'resetsAt', 'resets_at', 'rate_limit_type', 'utilization') if k in value}}
            if not result_seen: raise BackendError('Claude stream ended without a completion receipt')
            return {'text': text, 'turn': {'id': turn, 'status': 'interrupted' if state.get('interrupted') else 'completed'},
                    'cumulative_usage': dict(self.total_token_usage[ident])}
        finally:
            try:
                if not result_seen and not state['provider_settled']:
                    # A local timeout is not a provider completion receipt.
                    await self._disconnect(ident)
            finally:
                state['running'] = False; state['done'].set(); state['tickets'].clear()
                state['interrupted'] = False

    async def _usage(self, ident, value):
        old = self.total_token_usage[ident]
        self.total_token_usage[ident] = {**old, **{k: max(v, old.get(k, 0)) for k, v in value.items()}}
        if self.sessions[ident]['usage_hook']:
            await maybe_await(self.sessions[ident]['usage_hook'](self.total_token_usage[ident]))

    async def interrupt(self, ident):
        state = self.sessions.get(ident)
        if not state: return {'requested': True, 'acknowledged': False, 'reason': 'session_owner_unavailable'}
        if state['running']:
            state['interrupted'] = True
            try:
                async with asyncio.timeout(self.shutdown_timeout):
                    if state['connected']:
                        await state['client'].interrupt()
                    await state['done'].wait()
            except TimeoutError:
                return {'requested': True, 'acknowledged': False, 'reason': 'provider_interrupt_timeout'}
            except Exception:
                return {'requested': True, 'acknowledged': False, 'reason': 'provider_interrupt_failed'}
        return {'requested': True, 'acknowledged': state['provider_settled'], 'turn_id': self.turn_ids.get(ident)}

    async def settle_tools(self, ident, *, cancel=False, timeout=10):
        state = self.sessions[ident]
        active = {t for t in state['active'] if t is not asyncio.current_task() and not t.done()}
        if cancel:
            for task in active: task.cancel()
        if active: _, active = await asyncio.wait(active, timeout=timeout)
        return {'acknowledged': not active, 'pending': len(active)}

    async def _close_session(self, ident, state):
        receipt = await self.interrupt(ident)
        owner = state.get('owner')
        owned_process = bool(owner or state['connected'] or state.get('cleanup_unconfirmed'))
        if owner and not owner.done():
            if receipt.get('acknowledged'):
                await state['queue'].put(None)
            else:
                owner.cancel()
            try:
                async with asyncio.timeout(self.shutdown_timeout + .1):
                    await asyncio.shield(owner)
            except asyncio.CancelledError:
                if not owner.done():
                    raise
            except Exception:
                state['provider_settled'] = False
                state['cleanup_unconfirmed'] = True
                if not owner.done():
                    owner.cancel()
        if owned_process and (state.get('cleanup_unconfirmed') or not state['provider_settled']):
            raise BackendError('Claude process shutdown remains unconfirmed')

    async def close(self):
        results = await asyncio.gather(*(self._close_session(ident, state)
                                        for ident, state in list(self.sessions.items())), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise BackendError('Claude process shutdown remains unconfirmed')


class ClaudeDecisionBackend:
    """Official subscription runtime for the planner's public decision schema."""
    def __init__(self, state_dir, model, effort=None, *, auth=None):
        self.backend = ClaudeBackend(state_dir, auth=auth)
        self.model, self.effort = model, effort
        self.thread_id = None
        self.latest_usage = {}

    async def decide(self, instructions, prompt, images=None, *, on_text_delta=None):
        from .providers import DECISION_SCHEMA
        if not self.thread_id:
            self.thread_id = await self.backend.thread([], None, model=self.model,
                instructions=instructions + '\nRespond with exactly the requested JSON decision object.')
            self.backend.sessions[self.thread_id]['client'].options.output_format = {'type': 'json_schema', 'schema': DECISION_SCHEMA}
        baseline = dict(self.backend.total_token_usage[self.thread_id])
        result = await self.backend.run(self.thread_id, prompt, model=self.model, effort=self.effort)
        decision = json.loads(result['text'])
        if on_text_delta: on_text_delta(json.dumps(decision, ensure_ascii=False))
        self.latest_usage = {k: v - baseline.get(k, 0) for k, v in result['cumulative_usage'].items()}
        return decision, self.latest_usage

    async def interrupt(self):
        return await self.backend.interrupt(self.thread_id) if self.thread_id else {'acknowledged': True}

    async def close(self):
        await self.backend.close()
