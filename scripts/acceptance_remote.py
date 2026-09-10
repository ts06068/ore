"""Two actual host Codex workers against an isolated HTTP coordinator and browser."""
import asyncio,json,os,threading,time,uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx,uvicorn
from ore.config import Settings
from ore.engine import Engine
from ore.server import create_app
from ore.remote import worker

class Page(BaseHTTPRequestHandler):
 def do_GET(self):
  text='Remote worker '+self.path.strip('/')+' preserved this exact paragraph.'
  body=('<!doctype html><title>Remote fixture</title><p id="target">'+text+'</p>').encode()
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass

async def main():
 root=Path('.ore').resolve();state=root/'acceptance'/'remote-final'/uuid.uuid4().hex
 os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(root/'browsers');os.environ['LD_LIBRARY_PATH']=str(root/'browser-libs/usr/lib/x86_64-linux-gnu')
 fixture=ThreadingHTTPServer(('127.0.0.1',0),Page);threading.Thread(target=fixture.serve_forever,daemon=True).start()
 engine=Engine(Settings(state_dir=state,max_workers=0));engine.save_profile({'id':'local-fixture','allowed_hosts':['127.0.0.1'],'max_browser_sessions':2,'persist_session':False})
 server=uvicorn.Server(uvicorn.Config(create_app(engine),host='127.0.0.1',port=0,access_log=False,log_level='error'))
 serving=asyncio.create_task(server.serve());started=time.monotonic()
 while not server.started:await asyncio.sleep(0.05)
 origin='http://127.0.0.1:'+str(server.servers[0].sockets[0].getsockname()[1]);jobs=[]
 try:
  async with httpx.AsyncClient(base_url=origin,headers={'Authorization':'Bearer '+engine.settings.auth_token},timeout=30) as c:
   for i in (1,2):
    url=f'http://127.0.0.1:{fixture.server_port}/{i}'
    mission={'goal':f'Open {url} in the browser. Register exactly one included resource with id fixture-{i} and title Remote fixture {i}. Use page_extract with selector #target to save the exact paragraph as an excerpt, then finish. Do not delegate.',
     'urls':[url],'artifact_roles':['excerpt'],'access_profile_ref':'local-fixture','model_policy':'fixed','model':'gpt-6-astra','effort':'high','budget':{'max_turns':10,'max_seconds':240,'max_agent_workers':1},'limits':{'origin_min_interval_seconds':0}}
    response=await c.post('/v1/jobs',json=mission);response.raise_for_status();job=response.json();assert job['status']=='draft'
    await c.post(f"/v1/jobs/{job['id']}/run",json={});jobs.append((i,job['id']))
  await asyncio.wait_for(worker(origin,engine.settings.auth_token,parallel=2,once=True),600)
  results=[]
  for i,jid in jobs:
   job=engine.store.get_job(jid);artifacts=engine.store.artifacts(jid);contents=[Path(a['path']).read_text() for a in artifacts if a['role']=='excerpt']
   results.append({'job_id':jid,'status':job['status'],'passed':job['status']=='completed' and contents==[f'Remote worker {i} preserved this exact paragraph.'],'audit':engine.audit(jid),'worker_id':engine.store.tasks(jid)[0]['worker_id']})
  report={'passed':all(x['passed'] for x in results),'mode':'two_real_host_Codex_workers_to_authenticated_HTTP_coordinator','elapsed_seconds':time.monotonic()-started,'results':results,'model_calls':sum(e['type']=='agent_decision' for _,jid in jobs for e in engine.store.events(jid))}
  (root/'reports'/'remote-codex-acceptance.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
  if not report['passed']:raise RuntimeError('Remote Codex acceptance failed')
 finally:
  server.should_exit=True;await serving;fixture.shutdown()
asyncio.run(main())
