"""Chrome companion authentication, mission binding and actual local extension transport."""
import asyncio, json, socket, time, zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx, pytest, uvicorn
from playwright.async_api import async_playwright
from ore.engine import Engine
from ore.config import Settings
from ore.server import create_app
from ore.policy import AccessDenied
from test_browser import site, browser_dependencies
from test_server import client, login
from test_engine import engine

async def test_pairing_requires_operator_and_bounds_to_current_handoff(client,engine):
    job=engine.create({'goal':'Companion fixture','urls':['https://example.org/article'],'allowed_origins':['https://example.org'],'artifact_roles':[]},queued=False)
    h=engine.handoffs.create(job['id'],'browser','Fixture',context={'url':'https://example.org/article'})
    assert (await client.post('/v1/companion/pair',json={'handoff_id':h['id']})).status_code==401
    await login(client)
    paired=(await client.post('/v1/companion/pair',json={'handoff_id':h['id']})).json()
    assert paired['origins']==['https://example.org'] and paired['expires_at']>time.time()
    row=engine.companion.pairs[paired['pair_id']]
    assert paired['token'] not in json.dumps({k:v for k,v in row.items() if k!='pending'})
    assert not (await client.get('/v1/companion/pairs/'+paired['pair_id'])).json()['connected']
    assert (await client.post('/v1/companion/pairs/'+paired['pair_id']+'/attach',json={})).status_code==403
    package=await client.get('/v1/companion/package')
    with zipfile.ZipFile(BytesIO(package.content)) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['permissions']==['debugger']
        assert 'cookies' not in manifest['permissions']
        assert {'background.js','popup.html','protocol.js'}.issubset(archive.namelist())
    row['expires_at']=0
    assert not engine.companion.current(row)

async def test_socket_rejects_web_page_origin_without_consuming_pair(engine):
    ws=SimpleNamespace(headers={'origin':'https://attacker.invalid'},close=AsyncMock(),accept=AsyncMock())
    await engine.companion.socket(ws)
    ws.close.assert_awaited_once_with(code=4403)
    ws.accept.assert_not_awaited()

@pytest.mark.browser
async def test_real_chrome_extension_transport_and_cookie_local_download(tmp_path,site,browser_dependencies,monkeypatch):
    from test_browser import Handler
    original_get=Handler.do_GET
    def cookie_required(handler):
        if handler.path.startswith('/download') and 'ore_fixture=private-cookie' not in handler.headers.get('Cookie',''):
            handler.send_response(403);handler.send_header('Content-Length','0');handler.end_headers();return
        original_get(handler)
    monkeypatch.setattr(Handler,'do_GET',cookie_required)
    value=Engine(Settings(state_dir=tmp_path/'ore',auth_token='fixture-operator-companion',max_workers=0))
    value.save_profile({'id':'fixture','allow_private_network':True,'retrieval_policy':{'mode':'official_first','browser_fallback':True}})
    job=value.create({'goal':'Test actual Chrome extension transport','urls':[site+'/challenge'], 'allowed_origins':[site],
                     'access_profile_ref':'fixture','artifact_roles':[], 'limits':{'origin_min_interval_seconds':0}},queued=False)
    h=value.handoffs.create(job['id'],'browser','Local extension fixture',context={'url':site+'/challenge'})
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    server=uvicorn.Server(uvicorn.Config(create_app(value),log_level='error',lifespan='off'))
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:await asyncio.sleep(.02)
    extension=Path(__file__).resolve().parents[1]/'src/ore/companion_extension'
    try:
        async with async_playwright() as pw:
            context=await pw.chromium.launch_persistent_context(str(tmp_path/'chrome'),channel='chromium',headless=True,device_scale_factor=2,
                args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'])
            try:
                await context.add_cookies([{'name':'ore_fixture','value':'private-cookie','url':site,'httpOnly':True}])
                worker=context.service_workers[0] if context.service_workers else await context.wait_for_event('serviceworker',timeout=15000)
                extension_id=worker.url.split('/')[2]
                browser_errors=[]
                context.on('console',lambda message:browser_errors.append(message.text) if message.type=='error' else None)
                context.on('weberror',lambda error:browser_errors.append(str(error.error)))
                async with httpx.AsyncClient(base_url=base,headers={'Authorization':'Bearer fixture-operator-companion'},timeout=60) as c:
                    pair=(await c.post('/v1/companion/pair',json={'handoff_id':h['id']})).json()
                    popup=await context.new_page();await popup.goto(f'chrome-extension://{extension_id}/popup.html')
                    await popup.locator('#code').fill(json.dumps({**pair,'server':base}));await popup.locator('#connect').click()
                    for _ in range(100):
                        if (await c.get('/v1/companion/pairs/'+pair['pair_id'])).json()['connected']:break
                        await asyncio.sleep(.05)
                    assert value.companion.available(job['id']),{'popup':await popup.locator('#status').inner_text(),'errors':browser_errors,'listener':await asyncio.wait_for(worker.evaluate('chrome.runtime.onMessage.hasListeners()'),3),'page':await popup.evaluate("({scripts:[...document.scripts].map(s=>s.src),click:!!document.querySelector('#connect').onclick,manifest:chrome.runtime.getManifest().name})"),'worker':worker.url}
                    response=await c.post('/v1/companion/pairs/'+pair['pair_id']+'/attach',json={'expected_version':h['state_version'],'idempotency_key':'extension-fixture-attach'})
                    assert response.status_code==200,response.text
                    attached=response.json();session=value.browser.get(attached['session_id'])
                    assert session.context.companion and session.control=='human'
                    assert value.browser.browser is None # No coordinator browser launched.
                    frames=asyncio.Queue(maxsize=1);session.subscribers.add(frames)
                    frame=await asyncio.wait_for(frames.get(),5)
                    assert frame['type']=='frame' and frame['control']=='human'
                    assert 0<frame['width']<=1280 and 0<frame['height']<=800
                    session.subscribers.discard(frames)
                    with pytest.raises(AccessDenied):await session.context.cookies()
                    await value.browser.resume(session.id)
                    observation=await value.browser.observe(session.id,screenshot=True)
                    assert observation['challenge_detected'] and observation['image_url'].startswith('data:image/png;base64,')
                    assert session.last_status==200
                    await value.browser.challenge(session.id)
                    result=await value.browser.action(session.id,'click',{'epoch':session.epoch,'selector':'#solve','screenshot':False})
                    assert not result['challenge_detected']
                    assert value.store.get_challenge(session.challenge_id)['state']=='resolved'
                    from ore.tools import ToolRuntime
                    runtime=ToolRuntime(value,job['id'])
                    captured={'session_id':session.id,'control':'agent'}
                    await runtime._capture_browser(captured,{'completeness':'inventory'})
                    snapshot=value.coverage._get(job['id'],'snapshot',captured['snapshot_id'])
                    assert snapshot['status_code']==200 and snapshot['capture_kind'].startswith('rendered_dom_companion_sanitized')
                    session.policy.profile['retrieval_policy']['browser_fallback']=False
                    content,metadata=await session.context.download(site+'/download',10000)
                    assert content==b'name,value\nalpha,7\n' and metadata['status']==200
                    with pytest.raises(AccessDenied):await value.browser.action(session.id,'navigate',{'epoch':session.epoch,'url':'https://outside.invalid'})
                    assert not value.secrets.names() # Browser auth state never exported.
                    await popup.locator('#disconnect').click()
                    for _ in range(50):
                        if session.closed:break
                        await asyncio.sleep(.05)
                    assert session.closed
                    assert value.store.get_job(job['id'])['status']=='awaiting_user'
                    assert value.handoffs.list(job['id'],'active')
                    with pytest.raises(AccessDenied):await value.browser.create(job['id'],job['mission'],value.profile(job['mission']))
                    assert value.browser.browser is None
            finally:await context.close()
    finally:
        server.should_exit=True
        await serving
        await value.stop()

async def test_pair_token_single_use_and_expiry(engine):
    from starlette.websockets import WebSocketDisconnect
    job=engine.create({'goal':'Pair scope','allowed_origins':['https://example.org'],'artifact_roles':[]},queued=False)
    h=engine.handoffs.create(job['id'],'browser','Pair scope')
    pair=engine.companion.pair(h['id'])
    engine.browser.sessions={}
    class Socket:
        headers={'origin':'chrome-extension://'+'a'*32}
        def __init__(self,message):self.message=message;self.sent=[];self.closed=[]
        async def accept(self):pass
        async def close(self,code=1000):self.closed.append(code)
        async def send_json(self,data):self.sent.append(data)
        async def receive_json(self):
            if self.message is None:raise WebSocketDisconnect()
            value,self.message=self.message,None
            return value
    valid=Socket({'pair_id':pair['pair_id'],'token':pair['token']})
    await engine.companion.socket(valid)
    assert valid.sent[0]['type']=='connected'
    assert 'token' not in valid.sent[0]
    replay=Socket({'pair_id':pair['pair_id'],'token':pair['token']})
    await engine.companion.socket(replay)
    assert 4403 in replay.closed and not replay.sent
    pair2=engine.companion.pair(h['id']);row=engine.companion.pairs[pair2['pair_id']]
    row['expires_at']=0
    expired=Socket({'pair_id':pair2['pair_id'],'token':pair2['token']})
    await engine.companion.socket(expired)
    assert 4403 in expired.closed and not expired.sent
    malformed=Socket([])
    await engine.companion.socket(malformed)
    assert 4403 in malformed.closed

async def test_mission_revision_revokes_companion_commands(engine):
    job=engine.create({'goal':'Pair revision','allowed_origins':['https://example.org'],'artifact_roles':[]},queued=False)
    h=engine.handoffs.create(job['id'],'browser','Pair revision')
    pair=engine.companion.pair(h['id']);row=engine.companion.pairs[pair['pair_id']]
    row['ws']=SimpleNamespace(send_json=AsyncMock())
    await engine.revise(job['id'],{**job['mission'],'goal':'Changed mission'})
    with pytest.raises(AccessDenied):await engine.companion.call(row,'title')
    row['ws'].send_json.assert_not_awaited()
