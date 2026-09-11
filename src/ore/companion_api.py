"""Operator setup endpoints and a separately authenticated extension socket."""
from pathlib import Path
from io import BytesIO
import zipfile
from fastapi import APIRouter,Request,WebSocket
from fastapi.responses import Response
from .policy import AccessDenied
from .store import LeaseLost


def create_companion_router(engine):
    router=APIRouter(prefix='/v1/companion')
    hub=engine.companion

    @router.get('/package')
    async def package():
        output=BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for path in sorted((Path(__file__).parent/'companion_extension').iterdir()):
                if path.suffix in {'.js','.json','.html'}:archive.writestr(path.name,path.read_bytes())
        return Response(output.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="ore-chrome-companion.zip"'})

    @router.post('/pair')
    async def pair(request:Request):
        body=await request.json()
        return hub.pair(body['handoff_id'])

    @router.get('/pairs/{ident}')
    async def status(ident:str):
        row=hub.pairs.get(ident)
        if not row:raise KeyError(ident)
        return {'pair_id':ident,'connected':bool(row['ws']) and hub.current(row),'session_id':row['session_id'],'expires_at':row['expires_at']}

    @router.post('/pairs/{ident}/attach')
    async def attach(ident:str,request:Request):
        row=hub.pairs.get(ident)
        if not row or not row['ws'] or not hub.current(row):raise AccessDenied('Connect the Chrome extension first')
        body=await request.json()
        h=engine.handoffs.get(row['handoff_id'])
        if row['session_id']:
            if h.get('receipts',{}).get(body.get('idempotency_key'),{}).get('action')=='attach_companion':return engine.handoffs.public(h)
            raise AccessDenied('This connection already has an attached session')
        h,replay=engine.handoffs.begin_action(h['id'],'attach_companion',body['expected_version'],body['idempotency_key'])
        if replay:return engine.handoffs.public(h)
        new=None
        try:
            job=engine.store.get_job(row['job_id'])
            new=await engine.browser.create(job['id'],job['mission'],{**engine.profile(job['mission']),'require_companion':True},agent_id='task:'+h['task_id'] if h.get('task_id') else None)
            summary=await engine.browser.takeover(new.id)
            if h.get('checkpoint_url'):
                await engine.browser.action(new.id,'navigate',{'url':h['checkpoint_url'],'epoch':summary['epoch']},owner='human')
            await engine.browser.observe(new.id,owner='human',screenshot=True)
            old=engine.browser.sessions.get(h.get('session_id'))
            if old and not old.closed:await engine.browser.close_session(old.id)
            changes={'session_id':new.id,'session_state':'live','control_epoch':new.epoch,'status':'claimed','browser_transport':'chrome_companion'}
            return engine.handoffs.public(engine.handoffs.finish_action(h['id'],'attach_companion',body['idempotency_key'],changes))
        except Exception:
            if new and not new.closed:await engine.browser.close_session(new.id)
            engine.handoffs.finish_action(h['id'],'attach_companion',body['idempotency_key'],{'status':'needs_user','reason':'Chrome attachment failed. Inspect the dedicated tab and pair again.'},failed=True)
            raise

    @router.websocket('/socket')
    async def socket(ws:WebSocket):await hub.socket(ws)
    return router
