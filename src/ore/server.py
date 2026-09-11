"""Authenticated operator API, SSE journal and same-session browser broker."""
from __future__ import annotations
import asyncio
import base64
import contextlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from .engine import Engine
from .models import Rune
from .policy import AccessDenied, ModelPolicy, redact
from .store import LeaseLost, ControlConflict, DocumentConflict
from .handoffs import HandoffService, BrowserAccess, create_handoff_router
from .progress import progress_snapshot
from . import __version__
from .tools import ToolRuntime


def create_app(engine=None):
    engine=engine or Engine();remote_tools={}
    if not hasattr(engine,'handoffs'):engine.handoffs=HandoffService(engine.store)
    browser=BrowserAccess(engine)
    handoff_router=create_handoff_router(engine,browser)
    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        engine.handoffs.recover_interrupted_actions()
        yield
        if getattr(engine, 'connections', None):
            await engine.connections.close()
        await engine.stop()
    app=FastAPI(title='ORE',version=__version__,lifespan=lifespan);app.state.engine=engine
    def valid(token):return bool(token) and secrets.compare_digest(str(token),engine.settings.auth_token)
    def auth(headers,cookies):
        bearer=headers.get('authorization','')
        return valid(bearer[7:] if bearer.startswith('Bearer ') else cookies.get('ore_session',''))
    def origin_ok(origin,host):
        return not origin or urlsplit(origin).netloc==host
    @app.middleware('http')
    async def guard(request,call_next):
        if request.url.path.startswith('/v1/'):
            if request.method not in ('GET','HEAD','OPTIONS') and not origin_ok(request.headers.get('origin'),request.headers.get('host')):
                return JSONResponse({'detail':'Cross-origin mutations are disabled'},403)
            executor=getattr(engine,'execution',None)
            scoped_executor=bool(executor and executor.authorize_request(request))
            pool=getattr(engine,'pool',None)
            scoped_pool=bool(pool and pool.authorize_request(request))
            if request.url.path!='/v1/auth/login' and not (auth(request.headers,request.cookies) or scoped_executor or scoped_pool):
                return JSONResponse({'detail':'Operator authentication required'},401)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store' if request.url.path.startswith('/v1/') else 'no-cache'
        return response
    @app.exception_handler(RequestValidationError)
    async def protected_input_error(request, exc):
        if '/secret' in request.url.path or request.url.path.endswith('/login-code'):
            return JSONResponse({'detail': 'Invalid protected credential request'}, 422)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(AccessDenied)
    @app.exception_handler(ControlConflict)
    async def denied(request,exc):return JSONResponse({'detail':str(exc)},403)
    @app.exception_handler(DocumentConflict)
    @app.exception_handler(LeaseLost)
    async def lease(request,exc):return JSONResponse({'detail':str(exc)},409)
    @app.exception_handler(KeyError)
    async def missing(request,exc):return JSONResponse({'detail':'Requested record does not exist'},404)
    @app.exception_handler(__import__('playwright.async_api',fromlist=['Error']).Error)
    async def browser_error(request,exc):return JSONResponse({'detail':'Browser action could not complete; inspect the current session','code':type(exc).__name__},409)
    @app.exception_handler(__import__('ore.desktop', fromlist=['DesktopError']).DesktopError)
    async def desktop_error(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'desktop_runtime_unavailable'}, 503)
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'detail':str(exc)[:2000]},422)
    def job(ident):
        result=engine.store.get_job(ident)
        if result is None:raise KeyError(ident)
        return result
    @app.get('/healthz')
    async def health():return {'status':'ok','version':__version__}
    @app.post('/v1/auth/login')
    async def login(request:Request):
        body=await request.json()
        if not valid(body.get('token')):raise HTTPException(401,'Invalid operator token')
        response=JSONResponse({'authenticated':True})
        response.set_cookie('ore_session',engine.settings.auth_token,httponly=True,secure=request.url.scheme=='https',samesite='strict',max_age=43200)
        return response
    @app.post('/v1/auth/logout')
    async def logout():
        response=JSONResponse({'authenticated':False});response.delete_cookie('ore_session');return response
    @app.get('/v1/auth/status')
    async def auth_status():return {'authenticated':True,'codex_installed':bool(engine.backend.binary),'configured_secret_refs':engine.secrets.names()}
    @app.get('/v1/scheduler')
    async def scheduler():return engine.scheduler.snapshot()
    @app.get('/v1/jobs')
    async def jobs():return engine.store.list_jobs()
    @app.post('/v1/jobs',status_code=201)
    async def create(request:Request):
        body=await request.json();return engine.create(body.get('mission',body),body.get('rune'),queued=False)
    @app.get('/v1/jobs/{ident}')
    async def detail(ident:str):
        record=job(ident);audit=engine.audit(ident)
        requests=[await handoff_router.handoff_output(h) for h in engine.handoffs.list(ident,'active')]
        return {**record,'audit':audit,'tasks':engine.store.tasks(ident),'handoffs':requests,'href':f'/jobs/{ident}','progress':progress_snapshot(engine.store,record,requests,audit=audit.get('coverage',audit))}
    @app.get('/v1/jobs/{ident}/progress')
    async def progress(ident:str):
        record=job(ident);audit=engine.audit(ident)
        return progress_snapshot(engine.store,record,engine.handoffs.list(ident,'active'),audit=audit.get('coverage',audit))
    @app.get('/v1/jobs/{ident}/coverage')
    async def coverage(ident:str):
        record=job(ident);result={}
        for kind in ('collection','issue','article','binding','candidate'):
            rows=engine.store.list_documents('coverage.'+kind,job_id=ident)
            result[{'issue':'issues','article':'articles','binding':'bindings','candidate':'candidates'}.get(kind,kind)]=[redact({k:v for k,v in row.items() if k not in ('path','body','snapshot','html')}) for row in rows if row['revision']==record['revision'] and row['generation']==record['generation']]
        return result
    @app.patch('/v1/jobs/{ident}')
    async def revise(ident:str,request:Request):
        patch=await request.json();old=job(ident)['mission'];value={**old,**patch.get('mission',patch)}
        if 'instructions' in patch:value['goal']=patch['instructions']
        return await engine.revise(ident,value)
    @app.post('/v1/jobs/{ident}/{action}')
    async def action(ident:str,action:str):
        job(ident)
        if action=='pause':return await engine.pause(ident)
        if action in ('run','resume'):return await engine.run(ident)
        if action=='refresh':return await engine.refresh(ident)
        if action=='audit':return engine.audit(ident)
        raise HTTPException(404,'Unknown job action')
    @app.get('/v1/jobs/{ident}/resources')
    async def resources(ident:str):job(ident);return engine.store.resources(ident)
    @app.get('/v1/jobs/{ident}/artifacts')
    async def artifacts(ident:str):job(ident);return engine.store.artifacts(ident)
    @app.get('/v1/jobs/{ident}/events')
    async def events(ident:str,request:Request,after:int=0):
        job(ident)
        async def stream():
            cursor=after
            while not await request.is_disconnected():
                rows=await asyncio.to_thread(engine.store.events,ident,cursor)
                for row in rows:
                    cursor=row['id'];yield f'id: {cursor}\ndata: {json.dumps(row,ensure_ascii=False)}\n\n'
                if not rows:yield ': keepalive\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
    @app.get('/v1/jobs/{ident}/export')
    async def export(ident:str,format:str='jsonl'):
        rows=[{'type':'job','data':job(ident)},{'type':'audit','data':engine.audit(ident)}]
        for kind,values in [('resource',engine.store.resources(ident)),('artifact',engine.store.artifacts(ident)),('observation',engine.store.observations(ident))]:
            rows.extend({'type':kind,'data':redact({k:v for k,v in x.items() if k!='path'})} for x in values)
        if format=='json':return rows
        if format!='jsonl':raise HTTPException(422,'Supported export formats: json, jsonl')
        return StreamingResponse(iter([json.dumps(redact(r),ensure_ascii=False)+'\n' for r in rows]),media_type='application/x-ndjson',headers={'Content-Disposition':f'attachment; filename="ore-{ident}.jsonl"'})
    @app.get('/v1/jobs/{ident}/artifacts/{artifact_id}/file')
    async def artifact_file(ident:str,artifact_id:str):
        job(ident);item=next((x for x in engine.store.artifacts(ident) if x['id']==artifact_id),None)
        if not item:raise KeyError(artifact_id)
        path=Path(item['path']).resolve()
        if not path.is_relative_to((engine.settings.state_dir/'vault').resolve()):raise AccessDenied('Artifact path is outside the vault')
        return FileResponse(path,filename=Path(item.get('filename') or item['sha256']).name,media_type=item.get('media_type'))
    @app.get('/v1/models')
    async def models():
        try:return await engine.models()
        except Exception as exc:raise HTTPException(503,f'Model catalog unavailable: {type(exc).__name__}') from exc
    @app.get('/v1/capabilities')
    async def capabilities():
        return {'capabilities':engine.capabilities.catalog(),'plugin_errors':engine.capabilities.plugin_errors}
    @app.get('/v1/coverage-profiles')
    async def coverage_profiles():return engine.coverage.profiles()
    @app.post('/v1/coverage-profiles',status_code=201)
    async def register_coverage_profile(request:Request):
        return engine.coverage.register_profile(await request.json(),reviewer='operator')
    @app.get('/v1/sources')
    async def sources(access_profile_id:str='public'):
        from .source_policy import source_catalog
        return source_catalog(engine.profile({'access_profile_ref':access_profile_id}),engine.secrets.get)
    def rune_output(r):return {**r,'id':r.get('protocol_id'),'content':yaml.safe_dump(r,allow_unicode=True,sort_keys=False)}
    @app.get('/v1/runes')
    async def runes():return [rune_output(x) for x in engine.runes()]
    @app.post('/v1/runes/validate')
    async def validate_rune(request:Request):
        body=await request.json();data=yaml.safe_load(body['content']) if 'content' in body else body
        result=Rune.model_validate(data);return {'valid':True,'digest':result.digest,'rune':result.model_dump(mode='json')}
    @app.get('/v1/runes/{ident}')
    async def get_rune(ident:str):return rune_output(engine.rune(ident))
    @app.put('/v1/runes/{ident}')
    async def save_rune(ident:str,request:Request):
        body=await request.json();data=yaml.safe_load(body['content']) if 'content' in body else body
        return rune_output(engine.save_rune(ident,data))
    @app.get('/v1/access-profiles')
    async def profiles():return engine.profiles()
    @app.post('/v1/access-profiles')
    async def profile(request:Request):return engine.save_profile(await request.json())
    @app.put('/v1/secrets/{ref:path}')
    async def secret(ref:str,request:Request):
        body=await request.json();engine.secrets.set(ref,body['value']);return {'ref':ref,'stored':True}
    @app.get('/v1/browser/sessions')
    async def sessions():return await browser.sessions()
    @app.post('/v1/browser/sessions')
    async def open_browser(request:Request):
        body=await request.json()
        if body.get('job_id'):record=job(body['job_id'])
        else:
            from .operator_access import operator_mission
            conversation_id = body.get('conversation_id')
            if conversation_id and engine.store.get_document('conversation', conversation_id) is None:
                raise KeyError('Unknown conversation')
            record=engine.create(operator_mission(body.get('url'), profile=body.get('access_profile_ref','public'), conversation_id=conversation_id),queued=False)
        await engine.pause(record['id'],'awaiting_user')
        sid=await browser.create(record,engine.profile(record['mission']))
        summary=await browser.command(sid,'takeover')
        if body.get('url'):await browser.command(sid,'action',{'action':'navigate','url':body['url'],'epoch':summary['epoch'],'screenshot':False})
        handoff=engine.handoffs.create(record['id'],'browser','Operator browser session',session_id=sid,context={**summary,'url':body.get('url')})
        return {**(await browser.summary(sid)),'handoff_id':handoff['id'],'handoff_href':handoff['href']}
    @app.post('/v1/browser/{sid}/{action}')
    async def control(sid:str,action:str,request:Request):
        session=browser.session(sid)
        data=await request.json() if request.headers.get('content-length','0')!='0' else {}
        if action in ('input','secret-fill','secret-capture'):return await browser.command(sid,'action' if action=='input' else action,data)
        if action=='observe':return await browser.command(sid,'observe')
        if action=='takeover':
            await engine.pause(session.job_id,'awaiting_user')
            result=await browser.command(sid,'takeover',data)
            engine.handoffs.create(session.job_id,'browser','Operator control requested',session_id=sid,context=result)
            return result
        if action=='resume':
            requests=[h for h in engine.handoffs.list(session.job_id,'active') if h.get('session_id')==sid]
            if requests:raise HTTPException(409,{'message':'Resume through the durable handoff after verification','handoff_href':requests[0]['href']})
            if (await browser.command(sid,'observe')).get('challenge_detected'):raise AccessDenied('The browser still displays a challenge')
            result=await browser.command(sid,'resume',data);await engine.run(session.job_id);return result
        raise HTTPException(404,'Unknown browser action')
    @app.websocket('/v1/browser/{sid}/stream')
    async def browser_socket(ws:WebSocket,sid:str):
        protocols=ws.headers.get('sec-websocket-protocol','').split(',')
        token=''
        for protocol in protocols:
            p=protocol.strip()
            if p.startswith('ore.token.'):
                with contextlib.suppress(Exception):token=base64.urlsafe_b64decode(p[10:]+'='*(-len(p[10:])%4)).decode()
        if not (valid(token) or auth(ws.headers,ws.cookies)) or not origin_ok(ws.headers.get('origin'),ws.headers.get('host')):
            await ws.close(code=4403);return
        try:session=browser.session(sid)
        except AccessDenied:await ws.close(code=4404);return
        await ws.accept(subprotocol='ore.v1' if 'ore.v1' in [p.strip() for p in protocols] else None)
        queue=asyncio.Queue(maxsize=1);session.subscribers.add(queue)
        if session.frame:queue.put_nowait({**session.frame,'epoch':session.epoch,'control':session.control})
        async def output():
            while True:await ws.send_json(await queue.get())
        sender=asyncio.create_task(output())
        try:
            while True:
                data=await ws.receive_json()
                if data.get('type')!='input':continue
                try:await browser.command(sid,'action',data)
                except AccessDenied as exc:await ws.send_json({'type':'error','message':str(exc)})
        except (WebSocketDisconnect,RuntimeError):pass
        finally:
            sender.cancel();session.subscribers.discard(queue);await asyncio.gather(sender,return_exceptions=True)
    app.include_router(handoff_router)
    from .desktop_api import create_desktop_router
    app.include_router(create_desktop_router(engine))
    from .companion_api import create_companion_router
    app.include_router(create_companion_router(engine))
    from .connections import create_connection_router
    app.include_router(create_connection_router(engine,browser))
    from .onboarding import create_onboarding_router
    app.include_router(create_onboarding_router(engine,browser))
    if getattr(engine,'execution',None):
        from .execution import create_execution_router
        app.include_router(create_execution_router(engine.execution))
    if getattr(engine,'pool',None):
        from .pool import create_pool_router
        app.include_router(create_pool_router(engine.pool))
    from .conversation_api import attach_conversation_routes
    attach_conversation_routes(app,engine)
    from .worker_api import create_worker_router
    app.include_router(create_worker_router(engine))
    static=Path(os.environ.get('ORE_WEB_DIR',str(Path(__file__).parent/'static')))
    if not static.exists():static=Path('web/dist')
    if static.exists():
        @app.get('/chat')
        @app.get('/chat/{ident}')
        @app.get('/conversations/{ident}')
        @app.get('/jobs')
        @app.get('/jobs/{ident}')
        @app.get('/handoffs/{ident}')
        @app.get('/handoffs')
        @app.get('/connections')
        @app.get('/browser')
        @app.get('/runes')
        async def web_route():return FileResponse(static/'index.html')
        app.mount('/',StaticFiles(directory=static,html=True),name='web')
    else:
        @app.get('/')
        async def root():return {'name':'ORE','api':'/docs','web':'Build web/ or configure ORE_WEB_DIR'}
    return app
