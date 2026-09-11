#!/usr/bin/env python3
"""Real Astra performs a local iframe checkbox flow; NOT a production Cloudflare test."""
from __future__ import annotations
import argparse, asyncio, json, os, threading, time, uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from ore.config import Settings
from ore.engine import Engine
from ore.evaluation import runtime_fingerprint

TARGET_TEXT='The controlled journal issue contains one original research article and two supplementary files.'

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.server.widget:
            status=200
            body=f'''<!doctype html><html><body style="font:20px Arial;background:#f7f7f7;padding:22px">
            <label style="display:flex;align-items:center;gap:18px"><input type="checkbox" aria-label="Verify access" style="width:30px;height:30px"
            onchange="if(this.checked)parent.postMessage('fixture-access-checked','{self.server.main_origin}')">Verify access</label>
            <p style="font-size:14px">ORE controlled access-check fixture</p></body></html>'''
        elif 'ore_fixture_access=granted' in self.headers.get('Cookie',''):
            status=200
            body=f'<!doctype html><title>Controlled journal issue</title><main><h1>Journal issue</h1><p id="target">{TARGET_TEXT}</p></main>'
        else:
            status=403
            body=f'''<!doctype html><html><title>Just a moment...</title><body style="font:22px Arial;padding:60px">
            <div data-ore-challenge="true"><h1>Access verification</h1><p>Complete the visible checkbox to continue to the issue.</p>
            <iframe title="Access verification" src="{self.server.widget_origin}/widget" style="width:470px;height:145px;border:1px solid #888"></iframe></div>
            <script>addEventListener('message',event=>{{if(event.origin==='{self.server.widget_origin}'&&event.data==='fixture-access-checked'){{
            document.cookie='ore_fixture_access=granted; SameSite=Lax; Path=/';setTimeout(()=>location.reload(),700);}}}});</script></body></html>'''
        data=body.encode();self.server.responses.append({'path':self.path,'status':status})
        self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

async def main(args):
    assets=args.browser_assets.resolve()
    if (assets/'browsers').is_dir():os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(assets/'browsers')
    libraries=assets/'browser-libs/usr/lib/x86_64-linux-gnu'
    if libraries.is_dir():os.environ['LD_LIBRARY_PATH']=str(libraries)+(os.pathsep+os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else '')
    servers=[ThreadingHTTPServer(('127.0.0.1',0),Handler) for _ in range(2)]
    main_server,widget_server=servers
    main_origin=f'http://127.0.0.1:{main_server.server_port}';widget_origin=f'http://127.0.0.1:{widget_server.server_port}'
    threads=[]
    for server,widget in zip(servers,(False,True)):
        server.widget=widget;server.responses=[];server.main_origin=main_origin;server.widget_origin=widget_origin
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread)
    state=args.state_root.resolve()/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6])
    engine=Engine(Settings(state_dir=state,max_workers=0))
    engine.save_profile({'id':'controlled-access-check','allow_private_network':True,'browser_support_origins':[widget_origin],
        'challenge_success_selector':'#target','retrieval_policy':{'mode':'official_first','browser_fallback':True}})
    url=main_origin+'/issue'
    mission={'goal':f'Open {url} with the browser. Complete the ordinary visible access verification using ORE browser actions. Once the actual issue is visible, register one included resource titled Controlled journal issue with the observed URL and extract the exact #target paragraph using page_extract. Then finish. Do not delegate or fetch: this is a browser-interaction fixture. Use only observed controls; do not fabricate cookie values or URLs.',
        'urls':[url],'allowed_origins':[main_origin],'access_profile_ref':'controlled-access-check','sources':[],'artifact_roles':['excerpt'],
        'retrieval_policy':{'mode':'official_first','browser_fallback':True},'routing':{'mode':'fixed','model':'gpt-6-astra','effort':'high'},
        'limits':{'origin_min_interval_seconds':0},'on_challenge':{'policy_version':2,'mode':'auto','adaptive':True,'max_attempts_per_episode':3,'max_elapsed_seconds':120,'hard_max_attempts':6,'hard_max_elapsed_seconds':300,'recovery_settle_seconds':5},
        'budget':{'max_turns':14,'max_seconds':args.timeout,'max_agent_workers':1}}
    job=engine.create(mission);task=engine.store.claim_task('agent-challenge-fixture',job_id=job['id']);engine.store.update_job(job['id'],status='running')
    engine.challenge_service.start()
    started=time.monotonic();run=asyncio.create_task(engine.execute_task(task,'agent-challenge-fixture'));seen=0
    print(json.dumps({'event':'started','job_id':job['id'],'state':str(state),'real_model':'gpt-6-astra/high','production_cloudflare_test':False}),flush=True)
    try:
        while not run.done():
            if time.monotonic()-started>args.timeout+30:raise TimeoutError('Fixture execution deadline')
            events=engine.store.events(job['id']);decisions=[e for e in events if e['type']=='agent_decision']
            if len(decisions)!=seen:
                seen=len(decisions);print(json.dumps({'event':'progress','decisions':seen,'last_tool':decisions[-1]['payload']['decision']['tool'] if decisions else None}),flush=True)
            await asyncio.sleep(1)
        await run;engine.reconcile(job['id']);final=engine.store.get_job(job['id']);events=engine.store.events(job['id']);artifacts=engine.store.artifacts(job['id'])
        texts=[Path(a['path']).read_text() for a in artifacts if a['role']=='excerpt']
        sessions=list(engine.browser.sessions.values());episodes=[engine.store.get_challenge(s.challenge_id) for s in sessions if s.challenge_id]
        decisions=[e['payload']['decision'] for e in events if e['type']=='agent_decision'];clicks=[json.loads(d['arguments']) for d in decisions if d['tool']=='browser_action' and json.loads(d['arguments']).get('action')=='click']
        handoffs=[e for e in events if e['type']=='browser_handoff']
        checks={'completed':final['status']=='completed','exact_target_text':texts==[TARGET_TEXT],'one_attempt_resolved':len(episodes)==1 and episodes[0]['attempts']==1 and episodes[0]['state']=='resolved',
                'agent_clicked':bool(clicks),'no_human_handoff':not handoffs,'initial_403_then_200':{r['status'] for r in main_server.responses if r['path']=='/issue'}=={403,200}}
        report={'passed':all(checks.values()),'checks':checks,'job_id':job['id'],'model':'gpt-6-astra','effort':'high','model_decisions':len(decisions),
                'elapsed_seconds':round(time.monotonic()-started,3),'clicks':clicks,'scripted_model_decisions':False,'manual_browser_interaction':False,
                'production_cloudflare_test':False,'runtime_digest':runtime_fingerprint()['digest'],'artifact_sha256':[a['sha256'] for a in artifacts]}
        path=state/'reports/agent-challenge.json';path.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'event':'finished',**report,'report':str(path)}),flush=True)
        return 0 if report['passed'] else 2
    finally:
        if not run.done():run.cancel();await asyncio.gather(run,return_exceptions=True)
        await engine.stop();engine.store.close()
        for server in servers:server.shutdown();server.server_close()
        for thread in threads:thread.join()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-root',type=Path,default=Path('.ore/acceptance-agent-challenge'));parser.add_argument('--timeout',type=int,default=240);parser.add_argument('--browser-assets',type=Path,default=Path('.ore'))
    options=parser.parse_args()
    if not 1<=options.timeout<=600:parser.error('--timeout must be between 1 and 600 seconds')
    raise SystemExit(asyncio.run(main(options)))
