"""Local Codex app-server transport. Authentication stays inside Codex."""
from __future__ import annotations
import asyncio
import json
import inspect
import os
import pathlib
import shutil
import tomllib
from typing import Any, Awaitable, Callable

class BackendError(RuntimeError):
    pass

ToolHandler = Callable[[str, dict], Awaitable[dict]]
EventHandler = Callable[[str, dict], Any]
ContextToolHandler = Callable[[dict, dict], Awaitable[dict]]

DISABLED_FEATURES = [
    'shell_tool','unified_exec','browser_use','browser_use_external','browser_use_full_cdp_access',
    'computer_use','apps','plugins','hooks','multi_agent','multi_agent_v2','image_generation',
    'in_app_browser','in_app_local_automation','remote_plugin','memories','skill_search',
    'skill_mcp_dependency_install','code_mode','code_mode_host','view_image','sleep_tool',
]

def controlled_config() -> dict:
    config: dict[str, Any] = {'web_search':'disabled', 'analytics.enabled':False,
                              'approval_policy':'never', 'sandbox_mode':'read-only'}
    config.update({f'features.{name}':False for name in DISABLED_FEATURES})
    root = pathlib.Path(os.environ.get('CODEX_HOME', str(pathlib.Path.home()/'.codex')))
    path = root/'config.toml'
    if path.exists():
        try:
            cfg = tomllib.loads(path.read_text())
            for name in cfg.get('mcp_servers', {}):
                config[f'mcp_servers.{name}.enabled'] = False
        except (OSError,tomllib.TOMLDecodeError) as exc:
            raise BackendError('Cannot inspect Codex MCP configuration safely') from exc
    return config

def scoped_native_config(base: dict | None = None) -> dict:
    """Only the scoped dynamic-tool orchestration host differs from legacy mode.

    Reapply the deny list instead of trusting a caller-mutated backend config.
    Native JS can compose dynamic tools; generated OS programs remain ORE tools.
    """
    config = {**(base or {}), **controlled_config()}
    config.update({'features.code_mode': True, 'features.code_mode_host': True})
    return config

class CodexBackend:
    def __init__(self, binary: str | None = None, cwd: str | None = None, on_event: EventHandler | None = None):
        self.binary = binary or os.environ.get('ORE_CODEX_BIN') or shutil.which('codex')
        self.cwd = str(pathlib.Path(cwd or os.getcwd()).resolve())
        self.on_event = on_event
        self.process = None
        self.pending: dict[int, asyncio.Future] = {}
        self.handlers: dict[str, ToolHandler] = {}
        self.turns: dict[str, asyncio.Future] = {}
        self.turn_ids: dict[str,str] = {}
        self.turn_completions: dict[tuple[str, str], asyncio.Event] = {}
        self.last_text: dict[str,str] = {}
        self.token_usage: dict[str,dict] = {}
        self.total_token_usage: dict[str,dict] = {}
        self._context_handlers: dict[str, ContextToolHandler] = {}
        self._usage_handlers: dict[str, Callable] = {}
        self._usage_tasks: dict[str, asyncio.Task] = {}
        self._usage_errors: dict[str, BaseException] = {}
        self._after_tool_handlers: dict[str, Callable] = {}
        self._turn_handlers: dict[str, Callable] = {}
        self.resumed_turn_status: dict[str, dict] = {}
        self._tool_tasks_by_thread: dict[str, set[asyncio.Task]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self.counter = 0
        self.reader_task = None
        self.stderr_task = None
        self.config = controlled_config()
        self._tool_tasks: set[asyncio.Task] = set()
        self._text_handlers: dict[str, Callable] = {}
        self._message_phases: dict[tuple[str, str], str] = {}
        self._native_backend: CodexBackend | None = None
        self._is_scoped_native = False
        self._start_lock = asyncio.Lock()
        self._account_listeners: set[Callable] = set()

    def scoped_native_backend(self):
        # The installed app-server chooses host availability at process start;
        # thread config alone cannot enable it. Keep this process separate from
        # legacy/planning sessions and never retrofit a running process.
        if self._is_scoped_native:
            return self
        if self._native_backend is None:
            def forward(method, params):
                if self.on_event: self.on_event(method, params)
            child = CodexBackend(binary=self.binary, cwd=self.cwd, on_event=forward)
            child.config = scoped_native_config(self.config)
            child._is_scoped_native = True
            self._native_backend = child
        return self._native_backend

    async def start(self):
        async with self._start_lock:
            return await self._start_once()

    async def _start_once(self):
        if self.process and self.process.returncode is None:
            return self
        if not self.binary:
            raise BackendError('Codex is not installed; install Codex and sign in with ChatGPT')
        args = [self.binary,'app-server','--listen','stdio://']
        for key,value in self.config.items():
            args += ['-c', f'{key}={json.dumps(value)}']
        self.process = await asyncio.create_subprocess_exec(*args, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            limit=32*1024*1024)
        self.reader_task = asyncio.create_task(self._read())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self.request('initialize',{'clientInfo':{'name':'ore','title':'ORE','version':'0.1.0'},
                                           'capabilities':{'experimentalApi':True}})
            await self.send({'method':'initialized'})
        except BaseException:
            await self.close()
            raise
        return self

    def add_account_listener(self, callback):
        self._account_listeners.add(callback)

    def remove_account_listener(self, callback):
        self._account_listeners.discard(callback)

    async def account_request(self, method, params=None):
        allowed = {'account/read', 'account/rateLimits/read', 'account/tokenUsage/read',
                   'account/login/start', 'account/login/cancel'}
        if method not in allowed:
            raise BackendError('Unsupported account operation')
        if method == 'account/login/start' and params != {'type': 'chatgptDeviceCode'}:
            raise BackendError('Only official device-code sign-in is available')
        await self.start()
        return await self.request(method, params or {}, timeout=25)

    def _account_event(self, method, params):
        # These notifications can contain identity or authentication details.
        # Private auth adapters consume them; never send them to public streams.
        for callback in tuple(self._account_listeners):
            try:
                result = callback(method, params)
                if inspect.isawaitable(result):
                    task = asyncio.create_task(result)
                    self._background_tasks.add(task)
                    def settled(task):
                        self._background_tasks.discard(task)
                        if not task.cancelled():
                            task.exception()  # consume private callback errors without logging account data
                    task.add_done_callback(settled)
            except Exception:
                pass

    async def _drain_stderr(self):
        # Never relay raw runtime logs: they may contain signed URLs or account details.
        while self.process and await self.process.stderr.readline():
            pass

    async def send(self, message: dict):
        if not self.process or self.process.returncode is not None:
            raise BackendError('Codex process is not running')
        self.process.stdin.write((json.dumps(message,separators=(',',':'))+'\n').encode())
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 60):
        self.counter += 1
        ident = self.counter
        fut = asyncio.get_running_loop().create_future()
        self.pending[ident] = fut
        try:
            await self.send({'id':ident,'method':method,'params':params})
            return await asyncio.wait_for(fut,timeout)
        finally:
            self.pending.pop(ident,None)

    async def _read(self):
        try:
            while raw := await self.process.stdout.readline():
                msg = json.loads(raw)
                if 'method' not in msg and 'id' in msg:
                    fut = self.pending.get(msg['id'])
                    if fut and not fut.done():
                        if 'error' in msg:
                            fut.set_exception(BackendError(str(msg['error'].get('message','RPC error'))))
                        else:
                            fut.set_result(msg.get('result',{}))
                    continue
                method, params = msg.get('method',''), msg.get('params',{})
                if 'id' in msg:
                    task = asyncio.create_task(self._server_request(msg))
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                    owner = params.get('threadId')
                    if owner:
                        owned = self._tool_tasks_by_thread.setdefault(owner, set())
                        owned.add(task)
                        task.add_done_callback(owned.discard)
                    continue
                if method.startswith('account/'):
                    self._account_event(method, params)
                    continue
                tid = params.get('threadId')
                if method == 'item/started' and tid and params.get('item', {}).get('type') == 'agentMessage':
                    item = params['item']
                    self._message_phases[(tid, item.get('id', ''))] = item.get('phase')
                if method == 'item/agentMessage/delta' and tid:
                    delta = params.get('delta', '')
                    handler = self._text_handlers.get(tid)
                    phase = self._message_phases.get((tid, params.get('itemId', '')))
                    if phase in (None, 'final_answer'):
                        self.last_text[tid] = self.last_text.get(tid,'') + delta
                        if handler: handler(delta)
                if method == 'thread/tokenUsage/updated' and tid:
                    usage = params.get('tokenUsage', {})
                    self.token_usage[tid] = usage.get('last', {})
                    total = usage.get('total')
                    if isinstance(total, dict):
                        self._record_usage(tid, total)
                if method == 'turn/started' and tid:
                    self.turn_ids[tid] = params.get('turn',{}).get('id','')
                    self.turn_completions.setdefault((tid, self.turn_ids[tid]), asyncio.Event())
                    self._notify_turn(tid, self.turn_ids[tid])
                if method == 'turn/completed' and tid:
                    completed_id = params.get('turn', {}).get('id') or self.turn_ids.get(tid)
                    if completed_id:
                        self.turn_completions.setdefault((tid, completed_id), asyncio.Event()).set()
                    future = self.turns.get(tid)
                    if future and not future.done():
                        future.set_result({'thread_id':tid,'turn':params.get('turn',{}),
                                           'text':self.last_text.get(tid,'')})
                if self.on_event:
                    self.on_event(method,params)
        except (asyncio.CancelledError,GeneratorExit):
            raise
        except Exception as exc:
            self._fail(BackendError(f'Codex transport failed: {type(exc).__name__}'))
        finally:
            self._fail(BackendError('Codex transport closed'))

    def _fail(self,exc):
        for fut in [*self.pending.values(),*self.turns.values()]:
            if not fut.done():
                fut.set_exception(exc)

    async def _server_request(self,msg):
        method,params = msg['method'],msg.get('params',{})
        try:
            if method == 'item/tool/call':
                handler = self.handlers.get(params['threadId'])
                if handler is None:
                    raise BackendError('No ORE tool handler for this thread')
                context_handler = self._context_handlers.get(params['threadId'])
                if context_handler:
                    # RPC request IDs are transport-local and are not stable effect IDs.
                    if not all(isinstance(params.get(key), str) and params[key]
                               for key in ('threadId', 'turnId', 'callId', 'tool')):
                        raise BackendError('Native tool call is missing its stable provider identity')
                    metadata = {key: params[key] for key in ('threadId', 'turnId', 'callId', 'tool')}
                    metadata['provider'] = 'codex'
                    value = await context_handler(metadata, params.get('arguments', {}))
                else:
                    value = await handler(params['tool'],params.get('arguments',{}))
                result = value if 'contentItems' in value else {
                    'success':not value.get('error'),
                    'contentItems':[{'type':'inputText','text':json.dumps(value,ensure_ascii=False)}]}
            elif method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                result = {'decision':'decline'}
            elif method == 'item/permissions/requestApproval':
                result = {'permissions':{},'scope':'turn'}
            elif method == 'tool/requestUserInput':
                result = {'answers':{}}
            else:
                await self.send({'id':msg['id'],'error':{'code':-32601,'message':'ORE does not permit this capability'}})
                return
        except asyncio.CancelledError:
            result = {'success': False, 'contentItems': [{'type': 'inputText', 'text': 'ORE tool execution interrupted'}]}
        except Exception as exc:
            # Native handlers may raise an application control exception. Its raw
            # message is not a public error channel and may include source data.
            message = type(exc).__name__ if self._context_handlers.get(params.get('threadId')) else str(exc)[:1000]
            result = {'success':False,'contentItems':[{'type':'inputText','text':message}]}
        await self.send({'id':msg['id'],'result':result})
        after = self._after_tool_handlers.get(params.get('threadId'))
        if method == 'item/tool/call' and after:
            # The provider must receive the completed tool receipt before a yield
            # interrupts its turn. Never await this callback inside the tool task.
            task = asyncio.create_task(after())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def models(self):
        await self.start()
        rows,cursor = [],None
        while True:
            params = {'limit':100,'includeHidden':False}
            if cursor: params['cursor'] = cursor
            result = await self.request('model/list',params)
            rows += result.get('data',[])
            cursor = result.get('nextCursor')
            if not cursor: return rows

    async def thread(self, tools: list[dict], handler: ToolHandler, *, model: str | None = None,
                     instructions: str = '', resume: str | None = None,
                     context_handler: ContextToolHandler | None = None,
                     on_usage: Callable | None = None, after_tool: Callable | None = None,
                     on_turn: Callable | None = None,
                     scoped_native: bool = False, usage_watermark: dict | None = None):
        await self.start()
        params = {'cwd':self.cwd,'sandbox':'read-only','approvalPolicy':'never',
                  'config':scoped_native_config(self.config) if scoped_native else self.config,'dynamicTools':tools,
                  'developerInstructions':instructions}
        if model: params['model'] = model
        if resume:
            params['threadId'] = resume
            if usage_watermark: self.total_token_usage[resume] = dict(usage_watermark)
            if on_usage: self._usage_handlers[resume] = on_usage
        result = await self.request('thread/resume' if resume else 'thread/start',params)
        tid = result['thread']['id']
        self.handlers[tid] = handler
        if context_handler: self._context_handlers[tid] = context_handler
        if on_usage: self._usage_handlers[tid] = on_usage
        if after_tool: self._after_tool_handlers[tid] = after_tool
        if on_turn: self._turn_handlers[tid] = on_turn
        self.resumed_turn_status[tid] = {turn['id']: turn.get('status') for turn in result['thread'].get('turns', []) if turn.get('id')}
        return tid

    async def run(self, tid: str, prompt: str, *, model: str | None = None, effort: str | None = None,
                  timeout: float = 600, output_schema: dict | None = None, images: list[str] | None = None,
                  on_text_delta: Callable[[str], Any] | None = None):
        if tid in self.turns and not self.turns[tid].done():
            raise BackendError('A turn is already running')
        future = asyncio.get_running_loop().create_future()
        self.turns[tid] = future
        self.last_text[tid] = ''
        if on_text_delta is not None:
            self._text_handlers[tid] = on_text_delta
        usage_before = dict(self.total_token_usage.get(tid, {}))
        self._usage_errors.pop(tid, None)
        params = {'threadId':tid,'input':[{'type':'text','text':prompt}]}
        if images: params['input'] += [{'type':'image','url':image} for image in images]
        if model: params['model'] = model
        if effort: params['effort'] = effort
        if output_schema is not None: params['outputSchema'] = output_schema
        try:
            started = await self.request('turn/start',params)
            self.turn_ids[tid] = started.get('turn',{}).get('id','')
            self.turn_completions.setdefault((tid, self.turn_ids[tid]), asyncio.Event())
            self._notify_turn(tid, self.turn_ids[tid])
            result = await asyncio.wait_for(asyncio.shield(future),timeout)
            finals = [x.get('text','') for x in result.get('turn',{}).get('items',[]) if x.get('type')=='agentMessage' and x.get('phase')=='final_answer']
            if finals: result['text'] = finals[-1]
            await self.flush_usage(tid)
            cumulative = self.total_token_usage.get(tid, {})
            # Legacy users still receive one turn's usage; native internal
            # completions must all count, rather than only tokenUsage.last.
            result['usage'] = {key: max(0, value - usage_before.get(key, 0))
                               for key, value in cumulative.items() if isinstance(value, (int, float))} if cumulative else self.token_usage.get(tid,{})
            result['cumulative_usage'] = dict(cumulative)
            return result
        except (TimeoutError,asyncio.CancelledError):
            await self.interrupt(tid)
            raise
        finally:
            self.turns.pop(tid,None)
            self._text_handlers.pop(tid, None)
            for key in [key for key in self._message_phases if key[0] == tid]:
                self._message_phases.pop(key, None)

    def _notify_turn(self, tid: str, turn_id: str):
        handler = self._turn_handlers.get(tid)
        if not handler: return
        try:
            handler(turn_id)
        except Exception as exc:
            # A late fenced metadata callback must not kill the RPC reader that
            # is needed to receive the provider's interruption acknowledgement.
            self._usage_errors.setdefault(tid, exc)
            task = asyncio.create_task(self.interrupt(tid))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    def _record_usage(self, tid: str, value: dict):
        previous = self.total_token_usage.get(tid, {})
        numeric = {key: int(amount) for key, amount in value.items()
                   if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 0}
        regressed = [key for key, amount in numeric.items() if amount < previous.get(key, 0)]
        cumulative = {**previous, **{key: max(amount, previous.get(key, 0)) for key, amount in numeric.items()}}
        self.total_token_usage[tid] = cumulative
        handler = self._usage_handlers.get(tid)
        if not handler: return
        prior_task = self._usage_tasks.get(tid)
        async def notify():
            if prior_task:
                try: await prior_task
                except Exception: pass  # Retain the first error, but account later usage.
            try:
                result = handler({**cumulative, **({'counter_regressed': regressed} if regressed else {})})
                if inspect.isawaitable(result): await result
            except Exception as exc:
                self._usage_errors.setdefault(tid, exc)
                task = asyncio.create_task(self.interrupt(tid))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
        self._usage_tasks[tid] = asyncio.create_task(notify())

    async def flush_usage(self, tid: str):
        while True:
            task = self._usage_tasks.get(tid)
            if task: await asyncio.shield(task)
            if task is self._usage_tasks.get(tid): break
        error = self._usage_errors.get(tid)
        if error: raise error

    async def settle_tools(self, tid: str, *, cancel: bool = False, timeout: float = 10):
        pending = {task for task in self._tool_tasks_by_thread.get(tid, set())
                   if task is not asyncio.current_task() and not task.done()}
        if cancel:
            for task in pending: task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout)
        return {'acknowledged': not pending, 'pending': len(pending), 'cancel_requested': cancel}

    async def interrupt(self, tid: str):
        turn_id = self.turn_ids.get(tid)
        receipt = {'requested': False, 'acknowledged': None, 'thread_id': tid, 'turn_id': turn_id}
        if not turn_id:
            return receipt
        settled = self.turn_completions.setdefault((tid, turn_id), asyncio.Event())
        if settled.is_set():
            return {**receipt, 'acknowledged': True}
        receipt.update(requested=True, acknowledged=False)
        try:
            await self.request('turn/interrupt', {'threadId': tid, 'turnId': turn_id}, timeout=10)
            # An accepted RPC is not proof that provider computation stopped.
            await asyncio.wait_for(settled.wait(), timeout=10)
            receipt['acknowledged'] = True
        except (BackendError, TimeoutError):
            pass
        return receipt

    async def close(self):
        if self._native_backend is not None:
            await self._native_backend.close()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(),5)
            except TimeoutError: self.process.kill(); await self.process.wait()
        waiting = [task for task in [self.reader_task, self.stderr_task, *self._tool_tasks,
                                   *self._usage_tasks.values(), *self._background_tasks] if task and task is not asyncio.current_task()]
        for task in waiting: task.cancel()
        if waiting: await asyncio.gather(*waiting, return_exceptions=True)
        self.process = None

    async def __aenter__(self): return await self.start()
    async def __aexit__(self,*args): await self.close()
