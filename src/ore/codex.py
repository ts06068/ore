"""Local Codex app-server transport. Authentication stays inside Codex."""
from __future__ import annotations
import asyncio
import json
import os
import pathlib
import shutil
import tomllib
from typing import Any, Awaitable, Callable

class BackendError(RuntimeError):
    pass

ToolHandler = Callable[[str, dict], Awaitable[dict]]
EventHandler = Callable[[str, dict], Any]

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
        self.last_text: dict[str,str] = {}
        self.token_usage: dict[str,dict] = {}
        self.counter = 0
        self.reader_task = None
        self.stderr_task = None
        self.config = controlled_config()
        self._tool_tasks: set[asyncio.Task] = set()

    async def start(self):
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
        await self.request('initialize',{'clientInfo':{'name':'ore','title':'ORE','version':'0.1.0'},
                                       'capabilities':{'experimentalApi':True}})
        await self.send({'method':'initialized'})
        return self

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
                    continue
                tid = params.get('threadId')
                if method == 'item/agentMessage/delta' and tid:
                    self.last_text[tid] = self.last_text.get(tid,'') + params.get('delta','')
                if method == 'thread/tokenUsage/updated' and tid:
                    self.token_usage[tid]=params.get('tokenUsage',{}).get('last',{})
                if method == 'turn/started' and tid:
                    self.turn_ids[tid] = params.get('turn',{}).get('id','')
                if method == 'turn/completed' and tid:
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
        except Exception as exc:
            result = {'success':False,'contentItems':[{'type':'inputText','text':str(exc)[:1000]}]}
        await self.send({'id':msg['id'],'result':result})

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
                     instructions: str = '', resume: str | None = None):
        await self.start()
        params = {'cwd':self.cwd,'sandbox':'read-only','approvalPolicy':'never',
                  'config':self.config,'dynamicTools':tools,
                  'developerInstructions':instructions}
        if model: params['model'] = model
        if resume: params['threadId'] = resume
        result = await self.request('thread/resume' if resume else 'thread/start',params)
        tid = result['thread']['id']
        self.handlers[tid] = handler
        return tid

    async def run(self, tid: str, prompt: str, *, model: str | None = None, effort: str | None = None,
                  timeout: float = 600, output_schema: dict | None = None, images: list[str] | None = None):
        if tid in self.turns and not self.turns[tid].done():
            raise BackendError('A turn is already running')
        future = asyncio.get_running_loop().create_future()
        self.turns[tid] = future
        self.last_text[tid] = ''
        params = {'threadId':tid,'input':[{'type':'text','text':prompt}]}
        if images: params['input'] += [{'type':'image','url':image} for image in images]
        if model: params['model'] = model
        if effort: params['effort'] = effort
        if output_schema is not None: params['outputSchema'] = output_schema
        try:
            started = await self.request('turn/start',params)
            self.turn_ids[tid] = started.get('turn',{}).get('id','')
            result = await asyncio.wait_for(asyncio.shield(future),timeout)
            finals = [x.get('text','') for x in result.get('turn',{}).get('items',[]) if x.get('type')=='agentMessage' and x.get('phase')=='final_answer']
            if finals: result['text'] = finals[-1]
            result['usage']=self.token_usage.get(tid,{})
            return result
        except (TimeoutError,asyncio.CancelledError):
            await self.interrupt(tid)
            raise
        finally:
            self.turns.pop(tid,None)

    async def interrupt(self, tid: str):
        turn_id = self.turn_ids.get(tid)
        if turn_id:
            try: await self.request('turn/interrupt',{'threadId':tid,'turnId':turn_id},timeout=10)
            except (BackendError,TimeoutError): pass

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(),5)
            except TimeoutError: self.process.kill(); await self.process.wait()
        for task in [self.reader_task,self.stderr_task,*self._tool_tasks]:
            if task: task.cancel()
        self.process = None

    async def __aenter__(self): return await self.start()
    async def __aexit__(self,*args): await self.close()
