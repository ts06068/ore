"""One-job Chrome companion transport. Browser credentials stay on the client PC."""
from __future__ import annotations
import asyncio, base64, copy, hashlib, json, re, secrets, time, uuid
from types import SimpleNamespace
from urllib.parse import urlsplit
from .policy import AccessDenied, AccessPolicy


def manual_policy(session):
    mission, profile = copy.deepcopy(session.mission), copy.deepcopy(session.policy.profile)
    for value in (mission, profile):
        value.setdefault('retrieval_policy', {})['browser_fallback'] = True
    return AccessPolicy(mission, profile)


class CompanionHub:
    def __init__(self, engine):
        self.engine = engine
        self.pairs = {}

    def pair(self, handoff_id):
        h = self.engine.handoffs.get(handoff_id)
        if not self.engine.handoffs.is_current(h) or h['status'] not in {'needs_user','claimed','waiting_external','recovering'}:
            raise AccessDenied('This handoff is not active')
        job = self.engine.store.get_job(h['job_id'])
        if self.engine.execution.enabled:
            raise AccessDenied('Chrome companion currently requires the local coordinator execution backend')
        for row in self.pairs.values():
            if row['job_id'] == job['id'] and row.get('ws') and self.current(row):
                raise AccessDenied('This mission already has a connected Chrome companion')
        self.pairs={k:v for k,v in self.pairs.items() if v.get('ws') or v['expires_at']>time.time()}
        ident, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        origins = job['mission'].get('allowed_origins') or job['mission'].get('scope', {}).get('origins', [])
        if not origins:
            origins = list({f'{urlsplit(u).scheme}://{urlsplit(u).netloc}' for u in job['mission'].get('urls', [])})
        if not origins:
            raise AccessDenied('A Chrome companion requires explicit mission origins')
        origins=sorted(set(origins)|set(job['mission'].get('scope',{}).get('asset_origins',[])))
        row = {'id':ident,'job_id':job['id'],'handoff_id':h['id'],'revision':job['revision'],
               'generation':job['generation'],'origins':origins,'expires_at':time.time()+300,
               'token_hash':hashlib.sha256(token.encode()).hexdigest(),'used':False,'ws':None,'pending':{},'session_id':None}
        self.pairs[ident] = row
        return {'pair_id':ident,'token':token,'expires_at':row['expires_at'],'origins':origins,'protocol':'ore.chrome.v1'}

    def current(self, row):
        job = self.engine.store.get_job(row['job_id'])
        return bool(job and job['revision']==row['revision'] and job['generation']==row['generation'] and time.time()<row['expires_at'])

    def selected(self,job_id):
        return any(p['job_id']==job_id and p.get('session_id') for p in self.pairs.values()) or any(h.get('browser_transport')=='chrome_companion' and self.engine.handoffs.is_current(h) for h in self.engine.handoffs.list(job_id))

    def available(self, job_id):
        return next((p for p in self.pairs.values() if p['job_id']==job_id and p.get('ws') and not p.get('session_id') and self.current(p)), None)

    async def call(self, row, operation, args=None):
        if not self.current(row) or not row.get('ws'):
            raise AccessDenied('Chrome companion disconnected, expired, or mission changed; pair again')
        ident = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        row['pending'][ident] = future
        try:
            await row['ws'].send_json({'id':ident,'operation':operation,'arguments':args or {}})
            return await asyncio.wait_for(future,90 if operation=='fetch_begin' else 20)
        except (TimeoutError, RuntimeError) as exc:
            if row.get('ws'):
                try:await row['ws'].close(code=4408)
                except RuntimeError:pass
            raise AccessDenied('Chrome companion command did not complete') from exc
        finally:
            row['pending'].pop(ident,None)

    async def socket(self, ws):
        # A website cannot act as the extension, even with an operator cookie.
        if not re.fullmatch(r'chrome-extension://[a-p]{32}', ws.headers.get('origin','')):
            await ws.close(code=4403);return
        await ws.accept()
        row = None
        try:
            hello = await asyncio.wait_for(ws.receive_json(),5)
            if not isinstance(hello,dict):
                await ws.close(code=4403);return
            row = self.pairs.get(hello.get('pair_id'))
            token = hello.get('token')
            if (not row or not isinstance(token,str) or row['used'] or not self.current(row)
                or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),row['token_hash'])
                or any(p is not row and p['job_id']==row['job_id'] and p.get('ws') and self.current(p) for p in self.pairs.values())):
                row = None
                await ws.close(code=4403);return
            row.update(used=True,ws=ws,expires_at=time.time()+3600)
            await ws.send_json({'type':'connected','origins':row['origins'],'expires_at':row['expires_at']})
            while self.current(row):
                data = await asyncio.wait_for(ws.receive_json(),45)
                if not isinstance(data,dict):
                    await ws.close(code=4403);break
                if data.get('type')=='ping':
                    await ws.send_json({'type':'pong'});continue
                future = row['pending'].get(data.get('id'))
                if future and not future.done():
                    if data.get('error'):
                        future.set_exception(AccessDenied('Chrome companion operation failed: '+str(data['error'])[:200]))
                    else:
                        future.set_result(data.get('result'))
        except (TimeoutError, RuntimeError, ValueError):
            pass
        except __import__('starlette.websockets',fromlist=['WebSocketDisconnect']).WebSocketDisconnect:
            pass
        finally:
            if row:
                row['ws'] = None
                for future in row['pending'].values():
                    if not future.done():future.set_exception(AccessDenied('Chrome companion disconnected'))
                sid = row.get('session_id')
                session = getattr(self.engine.browser,'sessions',{}).get(sid)
                if session and not session.closed:
                    session.closed = True
                    self.engine.event(session.job_id,'browser_session_lost',{'session_id':sid,'reason':'companion_disconnected'})
                    await self.engine.pause(session.job_id,'awaiting_user')
                    task_id=session.agent_id.removeprefix('task:') if session.agent_id.startswith('task:') else None
                    h=self.engine.handoffs.create(session.job_id,'browser','Chrome disconnected. Pair the companion again to continue.',
                        session_id=sid,task_id=task_id,context={'url':session.page.url,'challenge_id':session.challenge_id})
                    self.engine.handoffs.update(h['id'],{'session_state':'lost','browser_transport':'chrome_companion'})
            try:await ws.close()
            except RuntimeError:pass


class CompanionLocator:
    def __init__(self,page,selector,index=None):self.page,self.selector,self.index=page,selector,index
    def nth(self,index):return CompanionLocator(self.page,self.selector,index)
    async def query(self,kind,**values):return await self.page.rpc('query',{'selector':self.selector,'index':self.index,'kind':kind,**values})
    async def count(self):return await self.query('count')
    async def inner_text(self,**kwargs):return await self.query('text')
    async def all_inner_texts(self):return await self.query('texts')
    async def evaluate_all(self,expression):return await self.page.rpc('elements')
    async def click(self,**kwargs):return await self.page.rpc('click',{'selector':self.selector,'index':self.index})
    async def fill(self,value,**kwargs):return await self.page.rpc('fill',{'selector':self.selector,'index':self.index,'text':value})
    async def evaluate(self,expression):raise AccessDenied('Enter login details directly in your Chrome tab')


class CompanionPage:
    def __init__(self,hub,row):
        self.hub,self.row,self.url,self.session=hub,row,'about:blank',None
        self.mouse=SimpleNamespace(click=lambda x,y:self.rpc('click',{'x':x,'y':y}),wheel=lambda x,y:self.rpc('scroll',{'deltaX':x,'deltaY':y}))
        self.keyboard=SimpleNamespace(press=lambda key:self.rpc('key',{'key':key}),insert_text=lambda text:self.rpc('type',{'text':text}))
    async def rpc(self,op,args=None):
        result=await self.hub.call(self.row,op,args)
        if not isinstance(result,dict):raise AccessDenied('Malformed Chrome companion response')
        url=result.get('url',self.url)
        if url!='about:blank':
            if f'{urlsplit(url).scheme}://{urlsplit(url).netloc}' not in self.row['origins']:
                raise AccessDenied('Chrome tab left the paired mission origins; return to the selected journal')
            if self.session.control=='human':policy=manual_policy(self.session)
            elif op.startswith('fetch_'):policy=AccessPolicy(self.session.mission,self.session.policy.profile,operation='download')
            else:policy=self.session.policy
            await policy.check(url)
        self.url=url
        status=result.get('http_status')
        self.session.last_status=status if type(status) is int and 100<=status<=599 else None
        return result.get('value')
    def locator(self,selector):return CompanionLocator(self,selector)
    async def title(self):return await self.rpc('title')
    async def content(self):return await self.rpc('content')
    async def evaluate(self,expression):
        if expression=='() => navigator.webdriver':return await self.rpc('webdriver')
        raise AccessDenied('Arbitrary scripts are not supported by the Chrome companion')
    async def goto(self,url,**kwargs):
        await self.rpc('navigate',{'url':url})
        # Only a captured top-frame network response establishes HTTP status.
        return SimpleNamespace(status=self.session.last_status) if self.session.last_status is not None else None
    async def screenshot(self,**kwargs):
        data=base64.b64decode(await self.rpc('screenshot'),validate=True)
        if len(data)<24 or len(data)>8*1024*1024 or not data.startswith(b'\x89PNG\r\n\x1a\n'):raise AccessDenied('Invalid companion screenshot')
        session=self.session
        session.frame_id+=1
        import struct
        width,height=struct.unpack('>II',data[16:24])
        if not 0<width<=1280 or not 0<height<=800:raise AccessDenied('Companion screenshot exceeds supported viewport')
        session.frame={'type':'frame','data':base64.b64encode(data).decode(),'width':width,'height':height,
                       'frame_id':session.frame_id,'epoch':session.epoch,'control':session.control,'scale':1}
        for queue in list(session.subscribers):
            if queue.full():queue.get_nowait()
            queue.put_nowait(dict(session.frame))
        return data
    async def wait_for_timeout(self,ms):await asyncio.sleep(ms/1000)
    async def go_back(self,**kwargs):await self.rpc('back')
    async def bring_to_front(self):pass


class CompanionContext:
    companion=True
    def __init__(self,page):self.pages=[page]
    async def close(self):
        if self.pages[0].row.get('ws'):
            await self.pages[0].hub.call(self.pages[0].row,'detach')
    async def cookies(self,*args):raise AccessDenied('Chrome cookies stay on the client; download through the companion')
    async def storage_state(self):return {'cookies':[],'origins':[]}
    async def download(self,url,limit):
        page=self.pages[0]
        await AccessPolicy(page.session.mission,page.session.policy.profile,operation='download').check(url)
        limit=min(limit,64*1024*1024)
        started=await page.rpc('fetch_begin',{'url':url,'limit':limit})
        ident=started['id'];size=started['bytes']
        if not isinstance(size,int) or size<0 or size>limit:raise AccessDenied('Companion file exceeded its byte limit')
        chunks=[]
        try:
            for offset in range(0,size,262144):
                part=await page.rpc('fetch_part',{'id':ident,'offset':offset})
                content=base64.b64decode(part,validate=True)
                if len(content)!=min(262144,size-offset):raise AccessDenied('Invalid companion file chunk')
                chunks.append(content)
        finally:
            await page.rpc('fetch_end',{'id':ident})
        return b''.join(chunks),started
