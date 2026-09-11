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
cards[1].update(job_id='setup1',access_profile_ref='connection-scopus',actions=['refresh','request_agent'],credential_pool_allowed=True,credential_pool=[{'id':'primary','enabled':True}])
form_actions=[{'id':'submit1','state_version':1,'status':'pending','kind':'click','url':'https://dev.elsevier.com/api-key','target':{'index':4,'label':'Create API key','tag':'button','type':'submit'}}]
registration_details={'given_name':'Ada','family_name':'Lovelace'}
requests=[]
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT/'dist'),**kwargs)
 def log_message(self,*args):pass
 def do_GET(self):
  path=urlsplit(self.path).path;requests.append(path)
  if path=='/healthz':return self.json({'status':'ok','version':'0.6.0rc1'})
  if path=='/v1/conversations':return self.json([conversation,{'id':'branch1','title':'Supplement follow-up','status':'completed','folder_id':'f1','branched_from':{'conversation_id':'c1','message_id':'a1'}},{'id':'branch2','title':'Supplement audit','status':'completed','folder_id':'f1','branched_from':{'conversation_id':'branch1','message_id':'a1'}}])
  if path=='/v1/conversation-folders':return self.json({'folders':[{'id':'f1','title':'Cardiology review'}]})
  if path=='/v1/conversations/c1':return self.json(deepcopy(conversation))
  if path=='/v1/connections':return self.json({'connections':deepcopy(cards)})
  if path=='/v1/connections/source1/form-actions':return self.json({'actions':deepcopy(form_actions)})
  if path=='/v1/connections/source1/enrollment/details':return self.json({'values':registration_details,'configured_fields':list(registration_details)})
  if path=='/v1/providers/status':return self.json({'providers':[{'provider':'codex','auth':{'status':'ready','plan_type':'Pro'},'quota':{'status':'available','windows':[{'id':'primary','remaining_percent':42,'window_minutes':300}],'ordinary_usage_allowed':True,'stale':False}}]})
  if path.endswith('/events'):
   body=('id: '+str(conversation['events_cursor'])+'\nevent: progress\ndata: '+json.dumps({'id':conversation['events_cursor'],'type':'progress','data':{'summary':'Local fixture progress updated.'}})+'\n\n').encode();self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  if path.startswith('/v1/'):return self.json([])
  if not (ROOT/'dist'/path.lstrip('/')).is_file():self.path='/index.html'
  return super().do_GET()
 def do_POST(self):
  path=urlsplit(self.path).path;requests.append('POST '+path);body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
  if path=='/v1/connections/source1/form-actions/submit1/approve':
   assert body['expected_version']==form_actions[0]['state_version']
   form_actions[0].update(status='completed',state_version=2);return self.json(form_actions[0])
  if path=='/v1/connections/source1/enrollment/secret':return self.json({'configured_protected_fields':['mfa_code']})
  if path=='/v1/connections/source1/enrollment/details':registration_details.update(body['values']);return self.json({'values':registration_details})
  return self.json({})
 def json(self,value):
  body=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);Thread(target=server.serve_forever,daemon=True).start()
checks={};errors=[]
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');page.on('pageerror',lambda error:errors.append(str(error)))
  page.goto(f'http://127.0.0.1:{server.server_port}/chat/c1');page.get_by_role('heading',name=conversation['title']).wait_for();page.get_by_role('article',name='Codex connection').wait_for();page.locator('.conversation-folder > summary').filter(has_text='Cardiology review').wait_for();page.get_by_text('v0.6.0rc1',exact=True).wait_for();checks['health_version']=True
  page.get_by_role('list',name='Branches of '+conversation['title']).get_by_role('list',name='Branches of Supplement follow-up').get_by_role('button',name='Supplement audit',exact=True).wait_for();checks['nested_conversation_branches']=True
  page.get_by_text('Model accounts and API quota',exact=True).click();page.get_by_text('42% remaining',exact=True).wait_for();checks['provider_quota_separate_from_budget']=page.get_by_label('Execution and budget').count()==1
  page.get_by_label('Verified included resources').first.wait_for() if page.get_by_label('Verified included resources').count() else None
  page.get_by_role('button',name='Approve action',exact=True).wait_for();checks['form_submission_requires_review']=not any(item.startswith('POST ') for item in requests);page.get_by_role('button',name='Approve action',exact=True).click();page.get_by_text('This action is complete.',exact=True).wait_for();checks['reviewed_form_action_completed']=True
  page.get_by_text('Registration details',exact=True).click();page.get_by_label('Given name',exact=True).wait_for();page.get_by_label('Institution or affiliation',exact=True).fill('Example University');page.get_by_role('button',name='Save registration details',exact=True).click();page.get_by_text('Registration details saved.',exact=True).wait_for();checks['chat_registration_details_saved']=registration_details['affiliation']=='Example University';page.get_by_text('Registration details',exact=True).click()
  checks['source_profile_not_selected_implicitly']=page.get_by_label('Chat access profile',exact=True).input_value()=='public';page.get_by_role('button',name='Use for this chat',exact=True).click();checks['source_profile_selected_in_chat']=page.get_by_label('Chat access profile',exact=True).input_value()=='connection-scopus'
  page.get_by_text('One-time verification code',exact=True).click();page.get_by_label('Provider verification code',exact=True).fill('654321');page.get_by_role('button',name='Save verification code',exact=True).click();page.get_by_text('Verification code saved for this setup step.',exact=True).wait_for();checks['verification_code_cleared']=page.get_by_label('Provider verification code',exact=True).input_value()=='';page.get_by_text('One-time verification code',exact=True).click()
  page.locator('.chat-transcript').evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(OUT/'desktop-light.png'),full_page=True)
  collection['resources'].update(complete=5,unresolved=1);collection['roles'][0].update(verified=5,missing=1);collection['gaps'].update(count=1,by_kind={'pending':1});conversation['events_cursor']=2;cards[0].update(status='ready',state_version=2,verification_url=None,user_code=None,message='Account connection verified.');conversation['runs'][0]['usage']['tokens']['totalTokens']=2400
  page.get_by_text('Account connection verified.',exact=True).wait_for(timeout=15000);page.get_by_text('5 verified',exact=False).first.wait_for(timeout=15000);checks['changing_progress_and_connection']=True
  checks['stream_reconnected']=requests.count('/v1/conversations/c1/events')>=2
  page.reload();page.get_by_text('Account connection verified.',exact=True).wait_for();page.get_by_text('5 verified',exact=False).first.wait_for();checks['reload_keeps_current_server_state']=True;page.get_by_text('This action is complete.',exact=True).wait_for();checks['form_action_not_replayed_on_reload']=requests.count('POST /v1/connections/source1/form-actions/submit1/approve')==1
  page.get_by_role('button',name='Switch to dark theme').click();page.set_viewport_size({'width':390,'height':844});page.locator('.chat-transcript').evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(OUT/'mobile-dark.png'),full_page=True);checks['mobile_no_overflow']=page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  page.get_by_role('button',name='Toggle navigation').click();page.locator('.conversation-folder > summary').filter(has_text='Cardiology review').wait_for(state='visible');page.keyboard.press('Escape');checks['mobile_sidebar_focus_restored']=page.get_by_role('button',name='Toggle navigation').evaluate('(e)=>document.activeElement===e')
  page.set_viewport_size({'width':1440,'height':1000});page.get_by_role('button',name='New chat',exact=True).click();page.get_by_label('Agent account',exact=True).select_option('openai');page.get_by_label('Message ORE',exact=True).fill('Collect five papers from Scopus');checks['unconfigured_api_collection_gated']=page.get_by_role('button',name='Send message',exact=True).is_disabled();page.get_by_label('Message ORE',exact=True).fill('Connect my OpenAI API account');checks['unconfigured_api_setup_enabled']=page.get_by_role('button',name='Send message',exact=True).is_enabled()
  checks['no_page_errors']=not errors;browser.close()
finally:server.shutdown()
report={'ok':all(checks.values()),'kind':'built_ui_with_changing_local_http_fixture','live_model_calls':0,'publisher_collection_verified':False,'checks':checks,'page_errors':errors};(OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report));raise SystemExit(0 if report['ok'] else 1)
