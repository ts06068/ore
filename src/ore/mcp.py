"""Local stdio MCP adapter: explicit status polling, never unsolicited chat push."""
from __future__ import annotations
import asyncio
import json
import os
import sys
from urllib.parse import quote
import httpx
from . import __version__
from .policy import redact


def schema(properties=None, required=None):
    return {'type':'object','properties':properties or {},'required':required or [],'additionalProperties':False}
STRING={'type':'string'}
CHAT_SETTINGS={'mode':{'type':'string','enum':['plan','execute']},
    'model_policy':{'type':'string','enum':['auto','fixed']},'model':STRING,'reasoning_effort':STRING}
CHAT_TOOLS=[
    {'name':'ore_conversation_list','description':'List durable ORE conversations.','inputSchema':schema()},
    {'name':'ore_conversation_create','description':'Start a planning conversation. Does not approve a plan or start collection.','inputSchema':schema({'title':STRING,**CHAT_SETTINGS})},
    {'name':'ore_conversation_get','description':'Read saved messages, questions, plans, public progress and run state. Poll again for updates.','inputSchema':schema({'conversation_id':STRING},['conversation_id'])},
    {'name':'ore_conversation_message','description':'Send an answer, progress question or instruction to an ORE conversation. Execute mode alone never approves an initial plan. A message does not resume stopped work.','inputSchema':schema({'conversation_id':STRING,'content':STRING,'change_and_continue':{'type':'boolean'},**CHAT_SETTINGS},['conversation_id','content'])},
    {'name':'ore_conversation_approve','description':'Approve a reviewed plan and run it when authorized by the user. This is the explicit initial execution gate.','inputSchema':schema({'conversation_id':STRING,'plan_id':STRING},['conversation_id','plan_id'])},
    {'name':'ore_conversation_interrupt','description':'Request Stop for conversation planning and execution. Inspect status until workers confirm pause. Completed results remain saved.','inputSchema':schema({'conversation_id':STRING},['conversation_id'])},
    {'name':'ore_conversation_resume','description':'Explicitly resume a stopped conversation from its saved checkpoints.','inputSchema':schema({'conversation_id':STRING},['conversation_id'])},
]
TOOLS=[
    {'name':'ore_status','description':'Read mission progress and durable needs_user links. Call again to see changes; this adapter does not push messages into chat.','inputSchema':schema({'job_id':STRING},['job_id'])},
    {'name':'ore_list_handoffs','description':'List active user intervention requests, including lost browser sessions and pending provider access.','inputSchema':schema({'job_id':STRING})},
    {'name':'ore_wait','description':'Poll a mission for up to 20 seconds, returning early when intervention is required or state changes.','inputSchema':schema({'job_id':STRING,'seconds':{'type':'number','minimum':0,'maximum':20}},['job_id'])},
    {'name':'ore_list_jobs','description':'List missions in this authenticated ORE workspace.','inputSchema':schema()},
    {'name':'ore_create_mission','description':'Create a draft mission for review. Does not start downloads.','inputSchema':schema({'mission':{'type':'object'}},['mission'])},
    {'name':'ore_job_action','description':'Explicitly run, pause, resume, refresh or audit a mission. Pending handoffs remain enforced by ORE.','inputSchema':schema({'job_id':STRING,'action':{'type':'string','enum':['run','pause','resume','refresh','audit']}},['job_id','action'])},
]+CHAT_TOOLS


class MCPAdapter:
    def __init__(self, server, token, *, client=None):
        self.server=server.rstrip('/')
        self.client=client or httpx.AsyncClient(base_url=self.server,headers={'Authorization':'Bearer '+token},timeout=30)

    async def request(self, method, path, body=None):
        response=await self.client.request(method,path,json=body)
        response.raise_for_status()
        return response.json()

    def links(self, value):
        if isinstance(value,list):return [self.links(item) for item in value]
        if isinstance(value,dict):
            result={key:self.links(item) for key,item in value.items()}
            for key in ('href','job_href','handoff_href','conversation_href'):
                if isinstance(result.get(key),str) and result[key].startswith('/'):
                    result[key]=self.server+result[key]
            return result
        return value

    async def call(self,name,args):
        ident=quote(str(args.get('job_id','')),safe='')
        if name.startswith('ore_conversation_'):
            tool=next((item for item in CHAT_TOOLS if item['name']==name),None)
            if tool is None:raise ValueError('Unknown conversation tool')
            from jsonschema import validate
            validate(args,tool['inputSchema'])
            conversation_id=quote(str(args.get('conversation_id','')),safe='')
            path='/v1/conversations'
            if name=='ore_conversation_list':value=await self.request('GET',path)
            elif name=='ore_conversation_create':value=await self.request('POST',path,args)
            else:
                if not conversation_id:raise ValueError('conversation_id is required')
                path+='/'+conversation_id
                if name=='ore_conversation_get':value=await self.request('GET',path)
                elif name=='ore_conversation_message':
                    if not args['content'].strip():raise ValueError('content must not be empty')
                    value=await self.request('POST',path+'/messages',{key:item for key,item in args.items() if key!='conversation_id'})
                elif name=='ore_conversation_approve':
                    plan=quote(args['plan_id'],safe='')
                    if not plan:raise ValueError('plan_id is required')
                    value=await self.request('POST',path+'/plans/'+plan+'/approve',{})
                elif name=='ore_conversation_interrupt':value=await self.request('POST',path+'/interrupt',{})
                else:value=await self.request('POST',path+'/resume',{})
        elif name=='ore_list_jobs':value=await self.request('GET','/v1/jobs')
        elif name=='ore_list_handoffs':value=await self.request('GET','/v1/handoffs?status=active'+('&job_id='+ident if ident else ''))
        elif name in ('ore_status','ore_wait'):
            if not ident:raise ValueError('job_id is required')
            value=await self.request('GET','/v1/jobs/'+ident)
            if name=='ore_wait':
                until=asyncio.get_running_loop().time()+max(0,min(float(args.get('seconds',15)),20))
                baseline=(value.get('status'),value.get('updated_at'))
                while not value.get('handoffs') and value.get('status') not in ('completed','failed','cancelled') and asyncio.get_running_loop().time()<until:
                    await asyncio.sleep(min(1,max(0,until-asyncio.get_running_loop().time())))
                    value=await self.request('GET','/v1/jobs/'+ident)
                    if (value.get('status'),value.get('updated_at'))!=baseline:break
            value={key:value[key] for key in ('id','status','href','revision','generation','progress','handoffs','audit') if key in value}
            value['needs_user']=bool(value.get('handoffs'))
        elif name=='ore_create_mission':value=await self.request('POST','/v1/jobs',{'mission':args['mission']})
        elif name=='ore_job_action':
            action=args.get('action')
            if not ident or action not in ('run','pause','resume','refresh','audit'):raise ValueError('Invalid mission action')
            value=await self.request('POST',f'/v1/jobs/{ident}/{action}',{})
        else:raise ValueError('Unknown ORE tool')
        return self.links(redact(value))

    async def handle(self,message):
        ident=message.get('id');method=message.get('method');params=message.get('params') or {}
        if ident is None:return None
        try:
            if method=='initialize':result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'ore','version':__version__},'instructions':'Use ore_conversation_create/message/get for multi-turn planning. Show the plan before explicit ore_conversation_approve. Stop uses ore_conversation_interrupt; inspect worker-confirmed status before reporting paused. Use ore_status or ore_wait to expose needs_user requests and their web links. Authentication, CAPTCHA and provider enrollment are completed by the user in the ORE web UI. Never request credentials in tool arguments.'}
            elif method=='ping':result={}
            elif method=='tools/list':result={'tools':TOOLS}
            elif method=='tools/call':
                try:
                    value=await self.call(params['name'],params.get('arguments') or {})
                    result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],'isError':False}
                except Exception as exc:
                    # Do not echo request URLs, headers, response bodies or credentials.
                    detail='ORE request failed: '+(str(exc.response.status_code) if isinstance(exc,httpx.HTTPStatusError) else type(exc).__name__)
                    result={'content':[{'type':'text','text':detail}],'isError':True}
            else:return {'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Method not found'}}
            return {'jsonrpc':'2.0','id':ident,'result':result}
        except Exception:return {'jsonrpc':'2.0','id':ident,'error':{'code':-32602,'message':'Invalid parameters'}}


def run_stdio(server: str, token: str | None = None):
    token=token or os.environ.get('ORE_AUTH_TOKEN','')
    if not token:raise ValueError('ORE_AUTH_TOKEN is required for the local MCP adapter')
    async def run():
        adapter=MCPAdapter(server,token)
        try:
            while line:=await asyncio.to_thread(sys.stdin.readline):
                try:message=json.loads(line)
                except json.JSONDecodeError:
                    print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}}),flush=True);continue
                if not isinstance(message,dict):continue
                result=await adapter.handle(message)
                if result is not None:print(json.dumps(result,ensure_ascii=False),flush=True)
        finally:await adapter.client.aclose()
    asyncio.run(run())
