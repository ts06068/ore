from __future__ import annotations
import asyncio
import ipaddress
import json
import math
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
        try:u=urlsplit(value)
        except ValueError:return '[invalid URL omitted]'
        query=[(k,'[redacted]' if any(s in k.lower() for s in ('key','token','signature','credential','auth','__cf_chl')) else v) for k,v in parse_qsl(u.query,keep_blank_values=True)]
        return urlunsplit((u.scheme,u.netloc,u.path,urlencode(query),''))
    return value

class AccessDenied(RuntimeError): pass

class DNSResolutionFailed(AccessDenied):
    """An otherwise admitted public request failed host resolution."""

    pass

def browser_resource_interval(mission, profile=None):
    """Bound page dependencies to 10 grants/second by default, per origin."""
    requested = float(mission.get('limits', {}).get('browser_resource_min_interval_seconds', 0.1))
    minimum = float((profile or {}).get('browser_resource_min_interval_seconds', 0.0))
    if not math.isfinite(requested) or not math.isfinite(minimum) or requested <= 0 or minimum < 0:
        raise AccessDenied('Browser resource interval must be finite and positive')
    return max(requested, minimum)


class AccessPolicy:
    def __init__(self,mission:dict,profile:dict|None=None,*,operation="browser",source_hint=None):
        self.mission=mission; self.profile=profile or {}
        self.operation=operation; self.source_hint=source_hint
        scope=mission.get('scope',{})
        self.origins=mission.get('allowed_origins') or scope.get('origins',[])
        self.assets=scope.get('asset_origins',[])
        self.profile_origins=self.profile.get('origins',[])
        self.allow_private=self.profile.get('allow_private_network',False)
        self.allow_hosts=self.profile.get('allowed_hosts',[])
    async def check_browser_request(self,url:str,*,top_level_url:str,is_top_level_navigation:bool,resource_type:str):
        """Permit explicitly configured support resources only inside an allowed page.

        This does not authorize top-level navigation, downloads, API operations or
        additional hosts. The normal profile, source and public-address checks
        still apply to both the embedding page and the requested resource.
        """
        origin=lambda value: f'{urlsplit(value).scheme}://{urlsplit(value).netloc}'
        support=set(self.mission.get('scope',{}).get('browser_support_origins',[])) | set(self.profile.get('browser_support_origins',[]))
        if (is_top_level_navigation or resource_type not in {'script','document','xhr','fetch','stylesheet','image','font','other'}
                or origin(url) not in support):
            return await self.check(url)
        await self.check(top_level_url)
        from .source_policy import operation_for_url
        if operation_for_url(url,'browser')!='browser':
            return await self.check(url)
        # Use a separate instance so support permission never leaks into the
        # regular download/navigation policy or another concurrent request.
        scoped=AccessPolicy(self.mission,self.profile,operation=self.operation,source_hint=self.source_hint)
        scoped.assets=[*self.assets,origin(url)]
        return await scoped.check(url)
    def browser_request_lane(self,url,*,top_level_url,is_top_level_navigation,resource_type):
        """Classify an already-authorized request; this never grants URL access."""
        from .source_policy import operation_for_url
        if is_top_level_navigation or operation_for_url(url,'browser') != 'browser':
            return 'main'
        resource, top = urlsplit(url), urlsplit(top_level_url)
        if top.scheme not in ('http','https') or not top.hostname:
            return 'main'
        if resource_type in {'script','stylesheet','image','font','media'}:
            return 'browser_resource'
        support = set(self.mission.get('scope',{}).get('browser_support_origins',[])) | set(self.profile.get('browser_support_origins',[]))
        origin = f'{resource.scheme}://{resource.netloc}'
        if origin in support and resource_type in {'document','xhr','fetch','other'}:
            return 'browser_resource'
        if (origin == f'{top.scheme}://{top.netloc}' and resource.path.startswith('/cdn-cgi/challenge-platform/')
                and resource_type in {'document','xhr','fetch','other'}):
            return 'browser_resource'
        return 'main'
    async def check(self,url:str):
        u=urlsplit(url)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password:
            raise AccessDenied('Only HTTP(S) URLs without embedded credentials are allowed')
        host=u.hostname.lower()
        from .source_policy import require_operation, source_for_url, operation_for_url, SourceUnavailable
        operation=operation_for_url(url,self.operation)
        source=source_for_url(url,self.profile,hint=self.source_hint)
        try:require_operation(self.mission,self.profile,source,operation,url=url)
        except SourceUnavailable as exc:raise AccessDenied(exc.result['reason']) from exc
        if self.profile_origins and f'{u.scheme}://{u.netloc}' not in self.profile_origins:
            raise AccessDenied('Origin outside access profile')
        if self.origins and f'{u.scheme}://{u.netloc}' not in [*self.origins,*self.assets]:
            raise AccessDenied(f'Origin outside the mission: {u.scheme}://{u.netloc}')
        if not self.allow_private and host not in self.allow_hosts:
            try:
                addresses=await asyncio.to_thread(socket.getaddrinfo,host,u.port or (443 if u.scheme=='https' else 80),0,socket.SOCK_STREAM)
            except socket.gaierror as exc: raise DNSResolutionFailed('DNS resolution failed') from exc
            if any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
                raise AccessDenied('Private or special network address requires an explicit access profile')
        return url

class RateLimiter:
    """Independent pacing lanes share one per-origin Retry-After cooldown."""
    def __init__(self,store=None):
        self.store=store
        self.lock=asyncio.Lock(); self.next={}; self.counts={}
        self.blocked_until={}; self.intervals={}; self.last_granted={}
    async def acquire(self,key:str,interval:float=3.0,*,lane='main'):
        if lane not in ('main','browser_resource') or not math.isfinite(interval) or interval < 0:
            raise ValueError('Invalid request pacing lane or interval')
        if self.store:
            options = {'lane': lane} if lane != 'main' else {}
            slot=await asyncio.to_thread(self.store.acquire_rate_slot,key,interval,**options)
            while True:
                await asyncio.sleep(slot['delay_seconds'])
                slot=await asyncio.to_thread(self.store.confirm_rate_slot,key,slot['slot_at'],**options)
                if slot['allowed']:return
        bucket=key if lane=='main' else (key,lane)
        async with self.lock:
            now=time.monotonic()
            self.intervals[bucket]=max(self.intervals.get(bucket,0),interval)
            slot=max(now,self.next.get(bucket,now),self.blocked_until.get(key,now))
            self.next[bucket]=slot+self.intervals[bucket]
            self.counts[bucket]=self.counts.get(bucket,0)+1
        while True:
            await asyncio.sleep(max(0,slot-time.monotonic()))
            async with self.lock:
                now=time.monotonic()
                slot=max(slot,self.blocked_until.get(key,0),self.last_granted.get(bucket,0)+self.intervals[bucket])
                if slot <= now:
                    self.last_granted[bucket]=now
                    return
    async def penalize(self,key:str,seconds:float):
        if self.store:
            await asyncio.to_thread(self.store.penalize_rate,key,max(0,seconds));return
        async with self.lock:
            blocked=max(self.blocked_until.get(key,0),time.monotonic()+max(0,seconds))
            self.blocked_until[key]=blocked
            self.next[key]=max(self.next.get(key,0),blocked)

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
