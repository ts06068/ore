from __future__ import annotations
from .credentials import CredentialUnavailable
import asyncio
import hashlib
import http.cookiejar
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit
import httpx
from bs4 import BeautifulSoup
from .contracts import validate_action
from .models import canonical_digest
from .routes import normalize_retrieval_policy, retrieval_plan
from .policy import AccessPolicy,GuardedTransport,AccessDenied,redact
from .source_policy import require_operation, source_for_url, operation_for_url, SourceUnavailable, scholarly_failure

TOOLS = {
 'state': 'Read current job, resource/artifact manifest, and child task states. Arguments: {}.',
 'browser_open': 'Create a dedicated browser. Arguments: {url?:str}. Returns session_id, screenshot and available text; desktop_chrome has OCR, no DOM.',
 'browser_observe': 'Observe browser screenshot and available text; desktop_chrome has OCR, no DOM. Arguments: {session_id:str}.',
 'browser_action': 'Operate browser. Arguments: {session_id:str,epoch:int,action:navigate|click|type|key|scroll|wait|tab|back,url?:str,target?:integer,selector?:str,x?:number,y?:number,text?:str,key?:str,deltaY?:number,index?:integer}. Use target indices from latest observation.',
 'search': 'Query a configured scholarly database. Scopus defaults to plain phrase text; set query_mode=native only for Scopus field syntax such as TITLE-ABS-KEY or DOCTYPE. Arguments: {source:str,query:str,query_mode?:plain|native,year_from?:int,year_to?:int,journals?:list,limit?:int,cursor?:str}. Persist returned records and cursor evidence.',
 'resolve': 'Find accessible main/attachment candidates for a known identifier. Arguments: {source:unpaywall|pmc,identifier:str}.',
 'fetch': 'Fetch an allowed URL and return text/links with provenance. Arguments: {url:str}.',
 'resource': 'Record an included/excluded/needs_review resource. Arguments: {id?:str,title:str,url?:str,doi?:str,classification?:str,article_type?:str,version?:str,supplement_status?:present|source_declares_none|not_listed_after_checks|unknown,supplement_evidence?:str,expected_supplements?:int,reason?:str}.',
 'download': 'Download and independently validate observed artifact URL. Arguments: {url:str,resource_id:str,role:main_pdf|supplement|full_text|attachment,filename?:str,doi?:str,title?:str,version?:str,session_id?:str,candidate_id?:str,requirement_id?:str}. Systematic jobs require candidate_id and requirement_id from sealed manifests.',
 'artifact_commit': 'Validate a file received through a browser download. Arguments: {session_id:str,download_index:int,resource_id:str,role:str,doi?:str,title?:str,version?:str}.',
 'archive_expand': 'Safely expand a verified ZIP, preserving the original and parent/member relations. Arguments: {artifact_id:str}.',
 'extract': 'Extract text/tables from a previously verified artifact. Arguments: {artifact_id:str}.',
 'page_extract': 'Extract exact text using a CSS selector in an observed browser. Arguments: {session_id:str,epoch:int,selector:str,resource_id:str}. Persists text artifact and evidence.',
 'seal_issue': 'Seal an official issue TOC using captured snapshot IDs and a trusted extraction profile. Arguments: {issue_id:str,snapshot_ids:list[str],profile_id:str}. Returns independently parsed article obligations.',
 'seal_article': 'Seal the official article type, main PDF and all supplements from captured snapshots and a trusted profile. Arguments: {article_key:str,snapshot_ids:list[str],profile_id:str}. Returns requirement_id/candidate_id for each file.',
 'inventory': 'Legacy count observation only; this cannot establish systematic completeness. Record finite official TOC reconciliation. Arguments: {expected_resources:int,enumeration_complete:bool,official_toc_evidence:list[str],gaps:list[str]}. Every evidence URL must have been observed.',
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
    def _received_bytes(self, job, amount, *, observation_id=None):
        """Account actual received bytes across v2 planning, resume and repair."""
        from .run_budget import RunBudget, BudgetExhausted
        scope = job['mission'].get('budget_scope_id') or job['id']
        if not self.engine.store.get_document(RunBudget.collection, scope):
            return  # Existing v1 accounting remains unchanged.
        budget = RunBudget(self.engine.store, scope)
        observed = budget.observe_bytes(amount, observation_id=observation_id)
        if observed['byte_overshoot']:
            raise BudgetExhausted('budget_bytes_exhausted', observed)

    async def execute(self,name,args):
        if name not in TOOLS:raise AccessDenied('Unknown ORE tool')
        if not isinstance(args,dict):raise AccessDenied('Tool arguments must be an object')
        args=validate_action(name,args)
        self.last_image=None
        job=self.job();mission=job['mission']
        from .connection_agent import assert_setup_action
        assert_setup_action(mission, name, args)
        if job['status'] in ('paused','cancelled','awaiting_user','awaiting_auth') and name not in ('state','finish'):
            raise AccessDenied('Job is paused; automatic actions are disabled')
        if self.task and job.get('revision',1)!=self.task.get('revision',job.get('revision',1)):
            raise AccessDenied('Mission revision changed; stop this attempt')
        if self.task:
            self.engine.store.heartbeat(self.task['id'],self.task['worker_id'],self.task['fence'],120)
        self.engine.event(self.job_id,'tool_started',{'tool':name,'arguments':redact(args),'task_id':self.task.get('id') if self.task else None})
        profile=self.engine.profile(mission)
        try:
            if name in ('browser_open','browser_action','challenge') and (
                    not normalize_retrieval_policy(mission,profile)['browser_fallback']
                    or not normalize_retrieval_policy({},profile)['browser_fallback']):
                raise SourceUnavailable({'allowed':False,'source':'general_web','operation':'browser',
                    'code':'browser_fallback_disabled','reason':'Automatic browser fallback is disabled; use available API or file routes.',
                    'required':False,'recoverable':True,'retryable':False,'fallback_allowed':True})
            if name in ('search','resolve'):
                require_operation(mission,profile,args['source'],name)
            elif args.get('url') and name in ('fetch','download','browser_open','browser_action'):
                operation=operation_for_url(args['url'],'browser' if name.startswith('browser') else 'download')
                require_operation(mission,profile,source_for_url(args['url'],profile),operation,url=args['url'])
            if name in ('download','artifact_commit') and mission.get('completeness') in ('inventory','systematic'):
                if not args.get('candidate_id') or not args.get('requirement_id'):
                    raise AccessDenied('A sealed candidate_id and requirement_id are required for systematic downloads')
                candidate=self.engine.coverage.check_download(self.job_id,args['candidate_id'],args['requirement_id'],url=args.get('url'),resource_id=args['resource_id'],role=args['role'])
                if args.get('url') and args['url']!=candidate['url']:raise AccessDenied('Download URL differs from the sealed candidate')
                if args['role']!=candidate['role']:raise AccessDenied('Download role differs from the sealed requirement')
                args['version']=candidate['version']
                if candidate['role']=='main_pdf' and candidate['article_key'].startswith('doi:'):args['doi']=candidate['article_key'][4:]
                elif candidate['role']=='supplement':
                    args.pop('doi',None);args.pop('title',None)
            execution=getattr(self.engine,'execution',None)
            from .execution import EXECUTOR_TOOLS
            if execution and execution.enabled and name in EXECUTOR_TOOLS:
                envelope=await execution.execute(self.task,self.job_id,name,args)
                result=envelope['result']
                if envelope.get('image_url'):result={**result,'image_url':envelope['image_url']}
            else:
                result=await self._execute(name,args,job,mission)
                if name in ('browser_open','browser_observe','browser_action'):
                    await self._capture_browser(result,mission)
        except SourceUnavailable as exc:
            result=exc.to_dict()
            self.engine.store.record_observation(self.job_id,{'kind':'source_unavailable',**result},lease=self.lease)
        except CredentialUnavailable as exc:
            result = scholarly_failure(exc, args.get('source', exc.source), name)
            result['resets_at'] = exc.resets_at
            self.engine.store.record_observation(self.job_id, {'kind': 'source_failure', **result}, lease=self.lease)
        except httpx.HTTPError as exc:
            result={'error':True,'recoverable':True,'code':'http_error','status':getattr(getattr(exc,'response',None),'status_code',None),'reason':type(exc).__name__,'fallback_allowed':True}
            self._record_download_outcome(args,result)
            self.engine.store.record_observation(self.job_id,{'kind':'retrieval_failure','url':args.get('url'),**result},lease=self.lease)
        except Exception as exc:
            from importlib.util import find_spec
            if find_spec('ore_scholarly'):
                from ore_scholarly.common import ScholarlyError
                if isinstance(exc,ScholarlyError):
                    result=scholarly_failure(exc,args.get('source','general_web'),name)
                    self.engine.store.record_observation(self.job_id,{'kind':'source_failure',**result},lease=self.lease)
                else:raise
            else:raise
        if isinstance(result,dict) and (result.get('control')=='human' or name=='handoff'):
            service=getattr(self.engine,'handoffs',None)
            if service:
                handoff=service.create(self.job_id,'challenge' if result.get('challenge_id') else 'browser',
                    args.get('reason','The browser requires your input.'),session_id=result.get('session_id') or args.get('session_id'),
                    task_id=self.task.get('id') if self.task else None,context=result)
                result={**result,'needs_user':True,'handoff_id':handoff['id'],'handoff_url':handoff['href']}
            else:result={**result,'needs_user':True}

        if isinstance(result,dict): self.last_image=result.pop('image_url',None)
        self.engine.event(self.job_id,'tool_completed',{'tool':name,'result':redact(result)})
        return result
    def coverage_state(self):
        e=self.engine;job=self.job()
        if job['mission'].get('completeness') not in ('inventory','systematic'):return None
        result={'profiles':[{k:p.get(k) for k in ('id','kind','journal_id','origins')} for p in e.coverage.profiles()]}
        for kind in ('collection','snapshot','issue','article','candidate','binding'):
            rows=e.store.list_documents('coverage.'+kind,job_id=self.job_id)
            result[kind]=[{k:v for k,v in row.items() if k not in ('path','seal_digest')} for row in rows if row['revision']==job['revision']]
        return result

    def _seal_resolver_candidates(self,source,result):
        candidates=result.get('candidates',[])
        if not candidates:return
        endpoint=result.get('provenance',{}).get('endpoint')
        if not endpoint:return
        # API results are explicitly identified as transformed resolver evidence,
        # never represented as original publisher HTML or as absence of supplements.
        groups={}
        for candidate in candidates:
            if candidate.get('role')!='main_pdf' or not candidate.get('doi'):continue
            tier='pmc' if source=='pmc' or source_for_url(candidate['url'])=='pmc' else 'publisher' if candidate.get('host_type')=='publisher' else None
            if tier:groups.setdefault(tier,[]).append(candidate)
        sealed=[]
        for tier,rows in groups.items():
            snapshot=self.engine.coverage.capture_snapshot(self.job_id,url=endpoint,
                content=json.dumps(redact(result),ensure_ascii=False).encode(),media_type='application/json',
                authority=tier,source_id=source,capture_kind='resolver_result',lease=self.lease)
            for candidate in rows:
                key='doi:'+candidate['doi'].lower().removeprefix('https://doi.org/')
                try:
                    sealed.extend(self.engine.coverage.register_candidates(self.job_id,key,[candidate],
                        source=tier,snapshot_ids=[snapshot['id']],lease=self.lease))
                except ValueError:
                    # Discovery before the authoritative article inventory is kept
                    # as a candidate, but never silently becomes an obligation.
                    continue
        result['sealed_candidates']=sealed

    def _record_download_outcome(self,args,result):
        if not args.get('candidate_id') or not args.get('requirement_id'):return
        candidate=self.engine.coverage.check_download(self.job_id,args['candidate_id'],args['requirement_id'])
        status=result.get('status')
        outcome='not_found' if status==404 else 'access_required' if status in (401,403) else 'temporarily_unavailable'
        for tier in candidate.get('covered_authorities',[candidate['authority']]):
            self.engine.coverage.record_attempt(self.job_id,candidate['article_key'],tier,outcome,
                requirement_id=args['requirement_id'],source_url=candidate['url'],lease=self.lease)

    async def _capture_browser(self,result,mission):
        if mission.get('completeness') not in ('inventory','systematic') or not isinstance(result,dict):return
        sid=result.get('session_id')
        if not sid or result.get('control')=='human':return
        session=self.engine.browser.get(sid)
        if getattr(session.context, 'desktop', False):
            result['coverage_capture'] = {'captured': False, 'reason': 'desktop_screenshot_is_not_original_html',
                'completeness_verified': False}
            return
        if not session.page.url.startswith(('https://','http://')):return
        # Credentials from login forms are never part of article evidence.
        if await session.page.locator('input[type="password"]:visible').count():return
        content=(await session.page.content()).encode()
        snapshot=self.engine.coverage.capture_snapshot(self.job_id,url=session.page.url,content=content,
            media_type='text/html',source_id=source_for_url(session.page.url),status_code=session.last_status if getattr(session.context,'companion',False) else session.last_status or 200,capture_kind='rendered_dom_companion_sanitized' if getattr(session.context,'companion',False) else 'rendered_dom',lease=self.lease)
        result['snapshot_id']=snapshot['id']

    async def _execute(self,name,args,job,mission):
        e=self.engine;s=e.store;profile=e.profile(mission)
        if name=='state':
            if e.execution.enabled:
                browsers=await e.execution.task_sessions(self.job_id,self.task['id'] if self.task else None)
            else:
                browsers=[await e.browser.summary(session) for session in getattr(e.browser,'sessions',{}).values()
                          if not session.closed and session.job_id==self.job_id and (not self.actor_id or session.agent_id==self.actor_id)]
            handoffs=[e.handoffs.public(row) for row in e.handoffs.list(self.job_id)
                      if e.handoffs.is_current(row) and (not self.task or row.get('task_id')==self.task['id'])]
            return {'job':job,'resources':s.resources(self.job_id),'artifacts':s.artifacts(self.job_id),
                    'tasks':s.tasks(self.job_id) if hasattr(s,'tasks') else [],'audit':e.audit(self.job_id),
                    'coverage':self.coverage_state(),'retrieval_plan':retrieval_plan(mission,profile),'browsers':browsers,'handoffs':handoffs[:20]}
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
            if name == 'search' and args.get('query_mode'):
                if args['source'] != 'scopus': raise ValueError('query_mode is currently supported only for Scopus')
                cfg['query_mode'] = args['query_mode']
            from .credentials import CredentialPool, CredentialTransport, QuotaObservingTransport
            credential_pool = CredentialPool(s, profile, e.secrets.get)
            pool_entries = credential_pool.entries(args['source'])
            if pool_entries:
                cfg['api_key_ref'] = credential_pool.select(args['source'], name)['ref']
            policy=AccessPolicy({**mission,'allowed_origins':[],'scope':{}},profile,operation=name,source_hint=args['source'])
            transport=GuardedTransport(policy,e.limiter,interval=float(profile.get('api_interval',1)),profile_id=profile['id'],transport=httpx.AsyncHTTPTransport(proxy=profile.get('proxy') or e.settings.browser_proxy))
            transport = CredentialTransport(transport, credential_pool, args['source'], e.limiter, name,
                float(profile.get('api_interval', 1))) if pool_entries else QuotaObservingTransport(transport, credential_pool, args['source'], name)
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
                    if 'candidates' in result:
                        result['candidates']=rank_candidates(result['candidates'],mission,profile)
                        if mission.get('completeness') in ('inventory','systematic'):
                            self._seal_resolver_candidates(args['source'],result)
            if hasattr(s,'record_observation'):s.record_observation(self.job_id,{'kind':name,'source':args['source'],'data':redact(result)},lease=self.lease)
            return result
        if name=='resource':return s.upsert_resource(self.job_id,args,lease=self.lease)
        if name in ('fetch','download'):
            download_mission=mission
            if args.get('candidate_id'):
                candidate=e.coverage.check_download(self.job_id,args['candidate_id'],args['requirement_id'],url=args['url'],resource_id=args['resource_id'],role=args['role'])
                origin=urlsplit(candidate['url']);allowed_origin=f'{origin.scheme}://{origin.netloc}'
                download_mission={**mission,'scope':{**mission.get('scope',{}),'asset_origins':[*mission.get('scope',{}).get('asset_origins',[]),allowed_origin]}}
            policy=AccessPolicy(download_mission,profile,operation='download');await policy.check(args['url'])
            cookies=[]
            if args.get('session_id'):
                session=e.browser.get(args['session_id'])
                if session.job_id!=self.job_id:raise AccessDenied('Browser belongs to another job')
                if self.actor_id and session.agent_id!=self.actor_id:raise AccessDenied('Browser belongs to another agent task')
                if session.control!='agent':raise AccessDenied('Browser is controlled by user')
                if getattr(session.context, 'companion', False) or getattr(session.context, 'desktop', False):
                    if name=='fetch':raise AccessDenied('Use browser_observe for Chrome pages and download for original files')
                    limit=int(mission.get('limits',{}).get('max_artifact_bytes',256*1024*1024))
                    await e.limiter.acquire(f"{profile['id']}:{urlsplit(args['url']).hostname}",session.interval)
                    content,metadata=await session.context.download(args['url'],limit)
                    self._received_bytes(job, len(content))
                    scope=f"{self.job_id}:{job['revision']}:{job['generation']}"
                    if not s.reserve_budget(scope,'download_bytes',len(content),mission.get('budget',{}).get('max_bytes',1_000_000_000))['allowed']:
                        raise AccessDenied('Download byte budget exceeded')
                    if metadata.get('url')!=args['url']:
                        raise AccessDenied('Browser download receipt does not match the requested URL')
                    chain = metadata.get('redirect_chain', [args['url']])
                    final_url = metadata.get('final_url', args['url'])
                    if not isinstance(chain, list) or not chain or chain[0] != args['url'] or chain[-1] != final_url:
                        raise AccessDenied('Browser download receipt contains an invalid redirect chain')
                    for target_url in chain:
                        await policy.check(target_url)
                    path=e.settings.state_dir/'staging'/uuid.uuid4().hex
                    path.write_bytes(content)
                    return self.commit(path,{**args,'_requested_source_url':args['url'],'_redirect_chain':chain},final_url)
                cookies=await session.context.cookies([args['url']])
            transport=GuardedTransport(policy,e.limiter,float(mission.get('limits',{}).get('origin_min_interval_seconds',3)),profile['id'],transport=httpx.AsyncHTTPTransport(proxy=profile.get('proxy') or e.settings.browser_proxy))
            jar=httpx.Cookies()
            for cookie in cookies:
                domain=cookie['domain'];expires=cookie.get('expires',-1)
                if '.' not in domain and ':' not in domain:domain=domain+'.local'
                jar.jar.set_cookie(http.cookiejar.Cookie(0,cookie['name'],cookie['value'],None,False,domain,domain.startswith('.'),domain.startswith('.'),cookie['path'],True,bool(cookie.get('secure')),int(expires) if expires>0 else None,expires<=0,None,None,{'HttpOnly':cookie.get('httpOnly',False)},False))
            async with httpx.AsyncClient(transport=transport,cookies=jar,timeout=90,follow_redirects=False) as client:
                url=args['url'];redirect_chain=[url]
                for _ in range(8):
                    request=client.build_request('GET',url)
                    response=await client.send(request,stream=True)
                    if response.is_redirect:
                        url=str(response.url.join(response.headers['location']));redirect_chain.append(url);await response.aclose();await policy.check(url);continue
                    break
                else:raise AccessDenied('Redirect limit exceeded')
                response.raise_for_status()
                limit=int(mission.get('limits',{}).get('max_artifact_bytes',256*1024*1024));chunks=[];received=0
                scope=f"{self.job_id}:{job['revision']}:{job['generation']}"
                try:
                    async for chunk in response.aiter_bytes():
                        received+=len(chunk)
                        self._received_bytes(job, len(chunk))
                        if received>limit or not s.reserve_budget(scope,'download_bytes',len(chunk),mission.get('budget',{}).get('max_bytes',1_000_000_000))['allowed']:
                            raise AccessDenied('Download byte budget exceeded')
                        chunks.append(chunk)
                    content=b''.join(chunks)
                finally:await response.aclose()
                if name=='fetch':
                    snapshot=None
                    if mission.get('completeness') in ('inventory','systematic'):
                        snapshot=e.coverage.capture_snapshot(self.job_id,url=str(response.url),content=content,media_type=response.headers.get('content-type','text/html'),source_id=source_for_url(str(response.url)),status_code=response.status_code,capture_kind='response_body',lease=self.lease)
                    soup=BeautifulSoup(content,'html.parser')
                    for tag in soup(['script','style','noscript']):tag.decompose()
                    # Bounded structured HTML supports deterministic extraction. Report
                    # truncation explicitly so a partial page cannot imply a full inventory.
                    for node in soup.select('input[value],button[value],option[value]'):
                        del node['value']
                    for node in soup.select('textarea'):node.clear()
                    html=str(soup);links=soup.find_all('a',href=True)
                    result={'url':str(response.url),'status':response.status_code,'text':soup.get_text(' ',strip=True)[:40000],
                            'html':html[:200000],'html_truncated':len(html)>200000,
                            'link_count':len(links),'links_truncated':len(links)>300,
                            'links':[{'text':a.get_text(' ',strip=True)[:200],'url':str(response.url.join(a['href']))} for a in links[:300]]}
                    if snapshot:result['snapshot_id']=snapshot['id']
                    if hasattr(s,'record_observation'):s.record_observation(self.job_id,{'kind':'page','url':str(response.url),'data':result},lease=self.lease)
                    return result
                path=e.settings.state_dir/'staging'/uuid.uuid4().hex
                path.write_bytes(content)
            return self.commit(path,{**args,'_requested_source_url':args['url'],'_redirect_chain':redirect_chain},str(response.url))
        if name=='artifact_commit':
            session=e.browser.get(args['session_id'])
            if session.job_id!=self.job_id:raise AccessDenied('Browser belongs to another job')
            if self.actor_id and session.agent_id!=self.actor_id:raise AccessDenied('Browser belongs to another agent task')
            item=session.downloads[int(args['download_index'])]
            self._received_bytes(job, Path(item['path']).stat().st_size, observation_id=f"browser:{args['session_id']}:{args['download_index']}")
            return self.commit(Path(item['path']),{**args,'filename':item['filename'],'_requested_source_url':item.get('requested_source_url',item['url']),'_redirect_chain':item.get('redirect_chain',[item['url']])},item['url'])
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
            extractor=e.vault.extract_document if hasattr(e.vault,'extract_document') else Forge(e.vault).extract
            result=await asyncio.to_thread(extractor,artifact)
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
        if name=='seal_issue':return e.coverage.seal_issue(self.job_id,args['issue_id'],args['snapshot_ids'],args['profile_id'],lease=self.lease)
        if name=='seal_article':return e.coverage.seal_article(self.job_id,args['article_key'],args['snapshot_ids'],args['profile_id'],lease=self.lease)
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
            result=await e.browser.takeover(args['session_id'])
            if not self.task:s.update_job(self.job_id,status='awaiting_user')
            return {**result,'reason':args.get('reason')}
        if name=='finish':return {'proposed':True,'summary':args.get('summary',''),'audit':e.audit(self.job_id)}
        raise AccessDenied('Unimplemented action')
    def commit(self,path,args,url):
        s=self.engine.store
        if not any(r['id']==args['resource_id'] for r in s.resources(self.job_id)):
            raise AccessDenied('Register the resource before attaching a file')
        filename=args.get('filename') or unquote(urlsplit(args.get('_requested_source_url') or args.get('url') or url).path.rsplit('/',1)[-1])
        expected={k:args[k] for k in ('role','doi','title','version') if args.get(k)}
        expected['filename']=filename
        if not Path(path).resolve().is_relative_to((self.engine.settings.state_dir/'staging').resolve()):raise AccessDenied('Only staged downloads can be committed')
        verified=self.engine.vault.commit_file(path,expected)
        record={**verified,'resource_id':args['resource_id'],'role':args['role'],'url':url,'source_url':url,
                'filename':filename,
                'version':args.get('version'),'source':'browser' if args.get('session_id') else 'http',
                'requested_source_url':args.get('_requested_source_url',url),'redirect_chain':args.get('_redirect_chain',[url])}
        artifact=s.add_artifact(self.job_id,record,lease=self.lease)
        if args.get('requirement_id') and args.get('candidate_id'):
            self.engine.coverage.bind_artifact(self.job_id,args['requirement_id'],args['candidate_id'],artifact,lease=self.lease)
        return artifact
