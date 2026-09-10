"""Real subscription Codex -> ORE -> Chromium -> verified exact text artifact."""
import asyncio,json,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from ore.engine import Engine
from ore.config import Settings
class Page(BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'<!doctype html><title>ORE acceptance source</title><h1>Research archive</h1><p id="target">The study enrolled 137 participants across four centers.</p><p>All follow-up visits occurred within 90 days.</p>'
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
async def main():
 server=ThreadingHTTPServer(('127.0.0.1',0),Page);threading.Thread(target=server.serve_forever,daemon=True).start()
 engine=Engine(Settings(max_workers=1));engine.save_profile({'id':'acceptance-local','name':'Explicit local test fixture','allowed_hosts':['127.0.0.1']})
 url=f'http://127.0.0.1:{server.server_port}/'
 job=engine.create({'goal':f'Open {url} in the browser. Extract the exact paragraph with CSS selector #target as an excerpt artifact. Register one included resource titled ORE acceptance source with the observed URL and use page_extract, then finish. Do not delegate; this is a one-resource fixture.',
  'urls':[url],'artifact_roles':['excerpt'],'access_profile_ref':'acceptance-local','routing':{'mode':'fixed','model':'gpt-6-astra','effort':'high'},'budget':{'max_turns':10,'max_seconds':600,'max_agent_workers':1},'limits':{'origin_min_interval_seconds':0}})
 print(json.dumps({'job_id':job['id'],'status':'started'}),flush=True);started=time.monotonic();seen=0
 try:
  await engine.run(job['id'])
  while engine.store.get_job(job['id'])['status'] in ('queued','running'):
   for event in engine.store.events(job['id'],seen):
    seen=event['id']
    if event['type'] in ('model_selected','agent_decision','agent_failed','tool_failed'):
     payload=event['payload'];print(json.dumps({'event':event['type'],'tool':payload.get('decision',{}).get('tool'),'model':payload.get('model'),'code':payload.get('code'),'message':payload.get('message')}),flush=True)
   await asyncio.sleep(1)
   if time.monotonic()-started>720:raise TimeoutError('Acceptance run exceeded deadline')
  final=engine.store.get_job(job['id']);artifacts=engine.store.artifacts(job['id']);audit=engine.audit(job['id'])
  texts=[Path(a['path']).read_text() for a in artifacts if a.get('role')=='excerpt']
  passed=final['status']=='completed' and texts==['The study enrolled 137 participants across four centers.']
  report={'passed':passed,'job_id':job['id'],'status':final['status'],'audit':audit,'texts':texts,'elapsed_seconds':time.monotonic()-started,'backend':'local Codex ChatGPT authentication','execution':'schema-constrained action turns; native tools disabled','models':[e['payload'] for e in engine.store.events(job['id']) if e['type']=='model_selected']}
  (engine.settings.state_dir/'reports'/'codex-acceptance.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
  if not passed:raise RuntimeError('Live agent acceptance failed')
 finally:await engine.stop();server.shutdown()
asyncio.run(main())
