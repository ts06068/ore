from __future__ import annotations
import asyncio
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
import httpx

SENSITIVE = {'password','passwd','authorization','cookie','set-cookie','api_key','apikey','token','secret','access_token','x-els-apikey','x-apikey'}

def redact(value):
    if isinstance(value,dict):
        return {k:('[redacted]' if k.lower().replace('-','_') in {'password','passwd','secret','token','api_key','apikey','cookie','cookies','set_cookie','authorization','access_token','refresh_token','x_els_apikey','x_apikey'} else redact(v)) for k,v in value.items()}
    if isinstance(value,list): return [redact(x) for x in value]
    if isinstance(value,str) and value.startswith(('https://','http://')):
        u=urlsplit(value)
        query=[(k,'[redacted]' if any(s in k.lower() for s in ('key','token','signature','credential','auth')) else v) for k,v in parse_qsl(u.query,keep_blank_values=True)]
        return urlunsplit((u.scheme,u.netloc,u.path,urlencode(query),''))
    return value

class AccessDenied(RuntimeError): pass

class AccessPolicy:
    def __init__(self,mission:dict,profile:dict|None=None):
        self.mission=mission; self.profile=profile or {}
        scope=mission.get('scope',{})
        self.origins=mission.get('allowed_origins') or scope.get('origins',[])
        self.assets=scope.get('asset_origins',[])
        self.profile_origins=self.profile.get('origins',[])
        self.allow_private=self.profile.get('allow_private_network',False)
        self.allow_hosts=self.profile.get('allowed_hosts',[])
    async def check(self,url:str):
        u=urlsplit(url)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:
            raise AccessDenied('Only HTTP(S) URLs without embedded credentials are allowed')
        host=u.hostname.lower()
        if self.profile_origins and f'{u.scheme}://{u.netloc}' not in self.profile_origins:
            raise AccessDenied('Origin outside access profile')
        if self.origins and f'{u.scheme}://{u.netloc}' not in [*self.origins,*self.assets]:
            raise AccessDenied(f'Origin outside the mission: {u.scheme}://{u.netloc}')
        if not self.allow_private and host not in self.allow_hosts:
            try:
                addresses=await asyncio.to_thread(socket.getaddrinfo,host,u.port or (443 if u.scheme=='https' else 80),0,socket.SOCK_STREAM)
            except socket.gaierror as exc: raise AccessDenied('DNS resolution failed') from exc
            if any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
                raise AccessDenied('Private or special network address requires an explicit access profile')
        return url

class RateLimiter:
    """Single coordinator owns reservations; workers obtain permits through its API."""
    def __init__(self,store=None):
        self.store=store
        self.lock=asyncio.Lock(); self.next:dict[str,float]={}
        self.counts:dict[str,int]={}
    async def acquire(self,key:str,interval:float=3.0):
        if self.store:
            slot=await asyncio.to_thread(self.store.acquire_rate_slot,key,interval)
            while True:
                await asyncio.sleep(slot['delay_seconds'])
                slot=await asyncio.to_thread(self.store.confirm_rate_slot,key,slot['slot_at'])
                if slot['allowed']:return
        async with self.lock:
            now=time.monotonic(); slot=max(now,self.next.get(key,now))
            self.next[key]=slot+max(interval,0)
            self.counts[key]=self.counts.get(key,0)+1
        await asyncio.sleep(max(0,slot-time.monotonic()))
    async def penalize(self,key:str,seconds:float):
        if self.store:
            await asyncio.to_thread(self.store.penalize_rate,key,max(0,seconds));return
        async with self.lock:
            self.next[key]=max(self.next.get(key,0),time.monotonic()+seconds)

class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self,policy:AccessPolicy,limiter:RateLimiter,interval:float=1,profile_id='public',transport=None):
        self.policy=policy; self.limiter=limiter; self.interval=interval; self.profile_id=profile_id
        self.transport=transport or httpx.AsyncHTTPTransport(retries=0)
    async def handle_async_request(self,request):
        await self.policy.check(str(request.url))
        key=f'{self.profile_id}:{request.url.host}'
        await self.limiter.acquire(key,self.interval)
        response=await self.transport.handle_async_request(request)
        if response.status_code==429:
            try: delay=float(response.headers.get('retry-after','30'))
            except ValueError: delay=30
            await self.limiter.penalize(key,min(delay,3600))
        return response
    async def aclose(self): await self.transport.aclose()

class ModelPolicy:
    def __init__(self,catalog:list[dict]):
        self.catalog={x.get('model',x.get('id')):x for x in catalog}
    def choose(self,mission:dict,kind='plan',failures=0,validated=False):
        routing=mission.get('routing') or {}
        mode=routing.get('mode',mission.get('model_policy','fixed'))
        requested=routing.get('model') or mission.get('model') or 'gpt-6-astra'
        effort=routing.get('effort') or mission.get('effort') or 'high'
        reason='user fixed profile'
        if mode in ('auto','quality_constrained_auto'):
            requested='gpt-6-astra'; effort='high'; reason='new or consequential task'
            if validated and kind in ('extract','classify') and failures==0:
                requested='gpt-5.6-terra'; effort='low'; reason='validated repetitive extraction profile'
            elif validated and kind=='retrieve' and failures==0:
                requested='gpt-5.6-sol'; effort='medium'; reason='validated retrieval profile'
            if failures:
                effort='xhigh'; reason='reasoning/validation failure escalated'
        row=self.catalog.get(requested)
        if row is None: raise AccessDenied(f'Model unavailable: {requested}; choose an available profile')
        efforts=[x['reasoningEffort'] for x in row.get('supportedReasoningEfforts',[])]
        if effort not in efforts: raise AccessDenied(f'Unsupported effort {effort} for {requested}: {efforts}')
        return {'model':requested,'effort':effort,'reason':reason,'policy_version':'1','mode':mode}
