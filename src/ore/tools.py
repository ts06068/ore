from __future__ import annotations
import asyncio
import hashlib
import http.cookiejar
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from bs4 import BeautifulSoup
from .contracts import validate_action
from .models import canonical_digest
from .policy import AccessPolicy,GuardedTransport,AccessDenied,redact

TOOLS = {
 'state': 'Read current job, resource/artifact manifest, and child task states. Arguments: {}.',
 'browser_open': 'Create a dedicated browser. Arguments: {url?:str}. Returns session_id, DOM and screenshot.',
 'browser_observe': 'Observe browser DOM and screenshot. Arguments: {session_id:str}.',
 'browser_action': 'Operate browser. Arguments: {session_id:str,epoch:int,action:navigate|click|type|key|scroll|wait|tab|back,url?:str,target?:integer,selector?:str,x?:number,y?:number,text?:str,key?:str,deltaY?:number,index?:integer}. Use target indices from latest observation.',
 'search': 'Query a configured scholarly database. Arguments: {source:str,query:str,year_from?:int,year_to?:int,journals?:list,limit?:int,cursor?:str}. Persist returned records and cursor evidence.',
 'resolve': 'Find accessible main/attachment candidates for a known identifier. Arguments: {source:unpaywall|pmc,identifier:str}.',
 'fetch': 'Fetch an allowed URL and return text/links with provenance. Arguments: {url:str}.',
 'resource': 'Record an included/excluded/needs_review resource. Arguments: {id?:str,title:str,url?:str,doi?:str,classification?:str,article_type?:str,version?:str,supplement_status?:present|source_declares_none|not_listed_after_checks|unknown,supplement_evidence?:str,expected_supplements?:int,reason?:str}.',
 'download': 'Download and independently validate observed artifact URL. Arguments: {url:str,resource_id:str,role:main_pdf|supplement|full_text|attachment,filename?:str,doi?:str,title?:str,version?:str,session_id?:str}.',
 'artifact_commit': 'Validate a file received through a browser download. Arguments: {session_id:str,download_index:int,resource_id:str,role:str,doi?:str,title?:str,version?:str}.',
 'archive_expand': 'Safely expand a verified ZIP, preserving the original and parent/member relations. Arguments: {artifact_id:str}.',
 'extract': 'Extract text/tables from a previously verified artifact. Arguments: {artifact_id:str}.',
 'page_extract': 'Extract exact text using a CSS selector in an observed browser. Arguments: {session_id:str,epoch:int,selector:str,resource_id:str}. Persists text artifact and evidence.',
 'inventory': 'Record finite official TOC reconciliation. Arguments: {expected_resources:int,enumeration_complete:bool,official_toc_evidence:list[str],gaps:list[str]}. Every evidence URL must have been observed.',
 'delegate': 'Create an independent child LLM-agent task for a nonoverlapping resource/collection. Arguments: {goal:str,key:str,kind?:plan|retrieve|extract|classify,urls?:list}.',
 'challenge': 'Record a challenge and reserve one shared bounded attempt. Arguments: {session_id:str,active_seconds?:number,resolved?:bool}. Never reset an unresolved episode.',
 'handoff': 'Pause automatic actions and give the browser to the user. Arguments: {session_id:str,reason:str}.',
 'finish': 'Propose task completion; ORE independently audits required artifacts. Arguments: {summary:str}.',
}

class ToolRuntime:
    def __init__(self,engine,job_id,task=None):
        self.engine=engine;self.job_id=job_id;self.task=task
        self.last_image=None
        self.actor_id=f"task:{task['id']}" if task else None
        self.lease={k:task[k] for k in ('worker_id','fence','revision')} | {'task_id':task['id']} if task else None
    def job(self): return self.engine.store.get_job(self.job_id)
    async def execute(self,name,args):
        if name not in TOOLS:raise AccessDenied('Unknown ORE tool')
        if not isinstance(args,dict):raise AccessDenied('Tool arguments must be an object')
        args=validate_action(name,args)
        self.last_image=None
        job=self.job();mission=job['mission']
        if job['status'] in ('paused','cancelled','awaiting_user','awaiting_auth') and name not in ('state','finish'):
            raise AccessDenied('Job is paused; automatic actions are disabled')
        if self.task and job.get('revision',1)!=self.task.get('revision',job.get('revision',1)):
            raise AccessDenied('Mission revision changed; stop this attempt')
        if self.task:
            self.engine.store.heartbeat(self.task['id'],self.task['worker_id'],self.task['fence'],120)
        self.engine.event(self.job_id,'tool_started',{'tool':name,'arguments':redact(args),'task_id':self.task.get('id') if self.task else None})
        result=await self._execute(name,args,job,mission)
        if isinstance(result,dict): self.last_image=result.pop('image_url',None)
        self.engine.event(self.job_id,'tool_completed',{'tool':name,'result':redact(result)})
        return result
    async def _execute(self,name,args,job,mission):
        e=self.engine;s=e.store;profile=e.profile(mission)
        if name=='state':
            return {'job':job,'resources':s.resources(self.job_id),'artifacts':s.artifacts(self.job_id),
                    'tasks':s.tasks(self.job_id) if hasattr(s,'tasks') else []}
        if name=='browser_open':
            session=await e.browser.create(self.job_id,mission,profile,agent_id=self.actor_id)
            if args.get('url'):return await e.browser.action(session.id,'navigate',args,owner_id=self.actor_id)
            return await e.browser.observe(session.id,owner_id=self.actor_id)
        if name in ('browser_observe','browser_action'):
            session=e.browser.get(args['session_id'])
            if session.job_id!=self.job_id:raise AccessDenied('Browser belongs to another job')
            if self.actor_id and session.agent_id!=self.actor_id:raise AccessDenied('Browser belongs to another agent task')
            if name=='browser_observe':return await e.browser.observe(session.id,owner_id=self.actor_id)
            return await e.browser.action(session.id,args['action'],args,owner_id=self.actor_id)
        if name in ('search','resolve'):
            from ore_scholarly import search,resolve
            if name=='search' and mission.get('sources') and args['source'] not in mission['sources']:raise AccessDenied('Search source outside mission')
            if name=='search':
                window=mission.get('publication_window',{})
                lower=int(window['from'][:4]) if window.get('from') else None
                upper=int(window['until_exclusive'][:4])-1 if window.get('until_exclusive','').endswith('-01-01') else None
                if lower is not None:
                    if args.get('year_from',lower)<lower:raise AccessDenied('Search date outside mission')
                    args.setdefault('year_from',lower)
                if upper is not None:
                    if args.get('year_to',upper)>upper:raise AccessDenied('Search date outside mission')
                    args.setdefault('year_to',upper)
                if mission.get('journals') and args.get('journals') and not set(args['journals']).issubset(set(mission['journals'])):raise AccessDenied('Search journals outside mission')
            cfg=dict(profile.get('sources',{}).get(args['source'],{}))
            cfg['secret_resolver']=e.secrets.get
            policy=AccessPolicy({'scope':{}},profile)
            transport=GuardedTransport(policy,e.limiter,interval=float(profile.get('api_interval',1)),profile_id=profile['id'],transport=httpx.AsyncHTTPTransport(proxy=profile.get('proxy') or e.settings.browser_proxy))
            async with httpx.AsyncClient(transport=transport,timeout=60,follow_redirects=False) as client:
                cfg['client']=client
                if name=='search':
                    result=await search(args['source'],args['query'],year_from=args.get('year_from'),year_to=args.get('year_to'),
                        journals=args.get('journals',mission.get('journals')),limit=min(int(args.get('limit',20)),200),cursor=args.get('cursor'),config=cfg)
                    checkpoint=canonical_digest({k:v for k,v in args.items() if k!='cursor'})
                    saved=s.commit_discovery(self.job_id,checkpoint,result.get('next_cursor'),[{**row,'classification':'needs_review'} for row in result.get('records',[])],[],revision=job['revision'],lease=self.lease)
                    result['records']=saved['resources'];result['checkpoint']=checkpoint
                else:
                    result=await resolve(args['source'],args['identifier'],config=cfg)
                    from .routes import rank_candidates
                    if 'candidates' in result:result['candidates']=rank_candidates(result['candidates'],mission,profile)
            if hasattr(s,'record_observation'):s.record_observation(self.job_id,{'kind':name,'source':args['source'],'data':redact(result)},lease=self.lease)
            return result
        if name=='resource':return s.upsert_resource(self.job_id,args,lease=self.lease)
        if name in ('fetch','download'):
            policy=AccessPolicy(mission,profile);await policy.check(args['url'])
            cookies=[]
            if args.get('session_id'):
                session=e.browser.get(args['session_id'])
                if session.job_id!=self.job_id:raise AccessDenied('Browser belongs to another job')
                if self.actor_id and session.agent_id!=self.actor_id:raise AccessDenied('Browser belongs to another agent task')
                if session.control!='agent':raise AccessDenied('Browser is controlled by user')
                cookies=await session.context.cookies([args['url']])
            transport=GuardedTransport(policy,e.limiter,float(mission.get('limits',{}).get('origin_min_interval_seconds',3)),profile['id'],transport=httpx.AsyncHTTPTransport(proxy=profile.get('proxy') or e.settings.browser_proxy))
            jar=httpx.Cookies()
            for cookie in cookies:
                domain=cookie['domain'];expires=cookie.get('expires',-1)
                jar.jar.set_cookie(http.cookiejar.Cookie(0,cookie['name'],cookie['value'],None,False,domain,domain.startswith('.'),domain.startswith('.'),cookie['path'],True,bool(cookie.get('secure')),int(expires) if expires>0 else None,expires<=0,None,None,{'HttpOnly':cookie.get('httpOnly',False)},False))
            async with httpx.AsyncClient(transport=transport,cookies=jar,timeout=90,follow_redirects=False) as client:
                url=args['url']
                for _ in range(8):
                    request=client.build_request('GET',url)
                    response=await client.send(request,stream=True)
                    if response.is_redirect:
                        url=str(response.url.join(response.headers['location']));await response.aclose();await policy.check(url);continue
                    break
                else:raise AccessDenied('Redirect limit exceeded')
                response.raise_for_status()
                limit=int(mission.get('limits',{}).get('max_artifact_bytes',256*1024*1024));chunks=[];received=0
                scope=f"{self.job_id}:{job['revision']}:{job['generation']}"
                try:
                    async for chunk in response.aiter_bytes():
                        received+=len(chunk)
                        if received>limit or not s.reserve_budget(scope,'download_bytes',len(chunk),mission.get('budget',{}).get('max_bytes',1_000_000_000))['allowed']:
                            raise AccessDenied('Download byte budget exceeded')
                        chunks.append(chunk)
                    content=b''.join(chunks)
                finally:await response.aclose()
                if name=='fetch':
                    soup=BeautifulSoup(content,'html.parser')
                    for tag in soup(['script','style','noscript']):tag.decompose()
                    result={'url':str(response.url),'status':response.status_code,'text':soup.get_text(' ',strip=True)[:40000],
                            'links':[{'text':a.get_text(' ',strip=True)[:200],'url':str(response.url.join(a['href']))} for a in soup.find_all('a',href=True)[:300]]}
                    if hasattr(s,'record_observation'):s.record_observation(self.job_id,{'kind':'page','url':str(response.url),'data':result},lease=self.lease)
                    return result
                path=e.settings.state_dir/'staging'/uuid.uuid4().hex
                path.write_bytes(content)
            return self.commit(path,args,str(response.url))
        if name=='artifact_commit':
            session=e.browser.get(args['session_id'])
            if session.job_id!=self.job_id:raise AccessDenied('Browser belongs to another job')
            if self.actor_id and session.agent_id!=self.actor_id:raise AccessDenied('Browser belongs to another agent task')
            item=session.downloads[int(args['download_index'])]
            return self.commit(Path(item['path']),{**args,'filename':item['filename']},item['url'])
        if name=='archive_expand':
            artifact=next((a for a in s.artifacts(self.job_id) if a['id']==args['artifact_id']),None)
            if not artifact or artifact.get('status')!='verified':raise AccessDenied('ZIP expansion requires a verified artifact')
            members=await asyncio.to_thread(e.vault.extract_zip,artifact['path'],parent_sha256=artifact['sha256'])
            return {'members':[s.add_artifact(self.job_id,{**member,'resource_id':artifact.get('resource_id'),'role':'supplement_member','source_artifact_id':artifact['id']},lease=self.lease) for member in members]}
        if name=='extract':
            from .forge import Forge
            artifact=next((a for a in s.artifacts(self.job_id) if a['id']==args['artifact_id']),None)
            if artifact is None:raise AccessDenied('Unknown artifact')
            if artifact.get('status')!='verified':raise AccessDenied('Extraction requires a verified original artifact')
            result=await asyncio.to_thread(Forge(e.vault).extract,artifact)
            for key in ('artifact','table_artifact'):
                if result.get(key):result[key]=s.add_artifact(self.job_id,{**result[key],'resource_id':artifact.get('resource_id')},lease=self.lease)
            return result
        if name=='page_extract':
            session=e.browser.get(args['session_id'])
            if session.job_id!=self.job_id or session.control!='agent':raise AccessDenied('Browser ownership mismatch')
            async with session.lock:
                await e.browser._authorize(session,'agent',args['epoch'],self.actor_id)
                text=await session.page.locator(args['selector']).all_inner_texts()
            path=e.settings.state_dir/'staging'/uuid.uuid4().hex;path.write_text('\n'.join(text))
            result=self.commit(path,{**args,'role':'excerpt','filename':'excerpt.txt'},session.page.url)
            s.record_observation(self.job_id,{'kind':'page_extraction','url':session.page.url,'selector':args['selector'],'artifact_id':result['id']},lease=self.lease)
            return {'text':text,'artifact':result}
        if name=='inventory':
            evidence=s.observations(self.job_id)
            observed={x.get('url') or x.get('source_url') or x.get('data',{}).get('url') for x in evidence}
            if not args['official_toc_evidence'] or any(url not in observed for url in args['official_toc_evidence']):raise AccessDenied('Inventory evidence must reference observed pages')
            return s.update_job(self.job_id,inventory=args,lease=self.lease)['inventory']
        if name=='delegate':
            if not args.get('goal') or not args.get('key'):raise AccessDenied('Delegation requires goal and unique scope key')
            if len(s.tasks(self.job_id))>=mission.get('budget',{}).get('max_tasks',10000):raise AccessDenied('Task budget exhausted')
            return s.create_task(self.job_id,args.get('kind','retrieve'),{'goal':args['goal'],'urls':args.get('urls',[])},args['key'],lease=self.lease)
        if name=='challenge':
            session=e.browser.get(args['session_id'])
            if session.job_id!=self.job_id:raise AccessDenied('Browser belongs to another job')
            if self.actor_id and session.agent_id!=self.actor_id:raise AccessDenied('Browser belongs to another agent task')
            if args.get('resolved'):return await e.browser.verify_challenge(session.id)
            return await e.browser.challenge(session.id)
        if name=='handoff':
            session=e.browser.get(args['session_id'])
            if session.job_id!=self.job_id:raise AccessDenied('Browser belongs to another job')
            if self.actor_id and session.agent_id!=self.actor_id:raise AccessDenied('Browser belongs to another agent task')
            result=await e.browser.takeover(args['session_id']);s.update_job(self.job_id,status='awaiting_user')
            return {**result,'reason':args.get('reason')}
        if name=='finish':return {'proposed':True,'summary':args.get('summary',''),'audit':e.audit(self.job_id)}
        raise AccessDenied('Unimplemented action')
    def commit(self,path,args,url):
        s=self.engine.store
        if not any(r['id']==args['resource_id'] for r in s.resources(self.job_id)):
            raise AccessDenied('Register the resource before attaching a file')
        expected={k:args[k] for k in ('role','doi','title','version') if args.get(k)}
        if not Path(path).resolve().is_relative_to((self.engine.settings.state_dir/'staging').resolve()):raise AccessDenied('Only staged downloads can be committed')
        verified=self.engine.vault.commit_file(path,expected)
        record={**verified,'resource_id':args['resource_id'],'role':args['role'],'url':url,'source_url':url,
                'filename':args.get('filename') or urlsplit(url).path.rsplit('/',1)[-1],
                'version':args.get('version'),'source':'browser' if args.get('session_id') else 'http'}
        return s.add_artifact(self.job_id,record,lease=self.lease)
