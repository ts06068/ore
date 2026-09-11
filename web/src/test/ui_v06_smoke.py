"""Built UI + changing local HTTP fixture, never a model or publisher acceptance."""
from copy import deepcopy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
import json
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'test-results'/'ui-v06'
OUT.mkdir(parents=True,exist_ok=True)
collection={'denominator':{'status':'sealed','basis':'Local UI fixture: six declared documents.','total':6},'resources':{'discovered':6,'included':6,'complete':2,'unresolved':4},'roles':[{'role':'main_pdf','expected':6,'verified':2,'missing':4,'unknown':0}],'issues':{'discovered':1,'complete':0,'total':1},'gaps':{'count':4,'by_kind':{'pending':4}}}
conversation={'id':'c1','title':'UI fixture · June collection','status':'running','execution_policy':'auto_within_scope','folder_id':'f1','messages':[{'id':'m1','role':'user','content':'Collect this local demonstration corpus.'},{'id':'a1','role':'assistant','operator_message_id':'m1','content':'The declared scope contains six documents. Collection is running.'}],'plans':[],'runs':[{'id':'r1','status':'running','operator_message_id':'m1','nodes':[{'id':'fetch','goal':'Collect declared files','status':'running'}],'collection_progress':collection,'execution_runtime':'native','usage':{'tokens':{'totalTokens':1200},'remaining_seconds':150,'remaining_tokens':48000,'usage_complete':True}}],'events':[],'events_cursor':1}
cards=[{'id':'codex1','provider':'codex','kind':'model','operation':'login','status':'awaiting_auth','state_version':1,'actions':['refresh'],'credential_fields':[],'configured_fields':[],'verification_url':'https://auth.openai.com/codex/device','user_code':'TEST-1234','message':'Finish signing in on the provider website.'},{'id':'source1','provider':'scopus','kind':'source','operation':'search','status':'pending','state_version':2,'actions':['refresh'],'credential_fields':['api_key'],'configured_fields':[],'message':'API approval is pending.'}]
requests=[]
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT/'dist'),**kwargs)
 def log_message(self,*args):pass
 def do_GET(self):
  path=urlsplit(self.path).path;requests.append(path)
  if path=='/healthz':return self.json({'status':'ok','version':'0.6.0rc1'})
  if path=='/v1/conversations':return self.json([conversation])
  if path=='/v1/conversation-folders':return self.json({'folders':[{'id':'f1','title':'Cardiology review'}]})
  if path=='/v1/conversations/c1':return self.json(deepcopy(conversation))
  if path=='/v1/connections':return self.json({'connections':deepcopy(cards)})
  if path=='/v1/providers/status':return self.json({'providers':[{'provider':'codex','auth':{'status':'ready','plan_type':'Pro'},'quota':{'status':'available','windows':[{'id':'primary','remaining_percent':42,'window_minutes':300}],'ordinary_usage_allowed':True,'stale':False}}]})
  if path.endswith('/events'):
   body=('id: '+str(conversation['events_cursor'])+'\nevent: progress\ndata: '+json.dumps({'id':conversation['events_cursor'],'type':'progress','data':{'summary':'Local fixture progress updated.'}})+'\n\n').encode();self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if path.startswith('/v1/'):return self.json([])
  if not (ROOT/'dist'/path.lstrip('/')).is_file():self.path='/index.html'
  return super().do_GET()
 def json(self,value):
  body=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);Thread(target=server.serve_forever,daemon=True).start()
checks={};errors=[]
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');page.on('pageerror',lambda error:errors.append(str(error)))
  page.goto(f'http://127.0.0.1:{server.server_port}/chat/c1');page.get_by_role('heading',name=conversation['title']).wait_for();page.get_by_role('article',name='Codex connection').wait_for();page.locator('.conversation-folder > summary').filter(has_text='Cardiology review').wait_for();page.get_by_text('v0.6.0rc1',exact=True).wait_for();checks['health_version']=True
  page.get_by_text('Model accounts and API quota',exact=True).click();page.get_by_text('42% remaining',exact=True).wait_for();checks['provider_quota_separate_from_budget']=page.get_by_label('Execution and budget').count()==1
  page.get_by_label('Verified included resources').first.wait_for() if page.get_by_label('Verified included resources').count() else None
  page.locator('.chat-transcript').evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(OUT/'desktop-light.png'),full_page=True)
  collection['resources'].update(complete=5,unresolved=1);collection['roles'][0].update(verified=5,missing=1);collection['gaps'].update(count=1,by_kind={'pending':1});conversation['events_cursor']=2;cards[0].update(status='ready',state_version=2,verification_url=None,user_code=None,message='Account connection verified.');conversation['runs'][0]['usage']['tokens']['totalTokens']=2400
  page.get_by_text('Account connection verified.',exact=True).wait_for(timeout=15000);page.get_by_text('5 verified',exact=False).first.wait_for(timeout=15000);checks['changing_progress_and_connection']=True
  checks['stream_reconnected']=requests.count('/v1/conversations/c1/events')>=2
  page.reload();page.get_by_text('Account connection verified.',exact=True).wait_for();page.get_by_text('5 verified',exact=False).first.wait_for();checks['reload_keeps_current_server_state']=True
  page.get_by_role('button',name='Switch to dark theme').click();page.set_viewport_size({'width':390,'height':844});page.locator('.chat-transcript').evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(OUT/'mobile-dark.png'),full_page=True);checks['mobile_no_overflow']=page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  page.get_by_role('button',name='Toggle navigation').click();page.locator('.conversation-folder > summary').filter(has_text='Cardiology review').wait_for(state='visible');page.keyboard.press('Escape');checks['mobile_sidebar_focus_restored']=page.get_by_role('button',name='Toggle navigation').evaluate('(e)=>document.activeElement===e')
  checks['no_page_errors']=not errors;browser.close()
finally:server.shutdown()
report={'ok':all(checks.values()),'kind':'built_ui_with_changing_local_http_fixture','live_model_calls':0,'publisher_collection_verified':False,'checks':checks,'page_errors':errors};(OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));raise SystemExit(0 if report['ok'] else 1)
