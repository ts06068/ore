"""Opt-in real Codex public-stream, Stop, reload and Resume browser acceptance. No collection is requested."""
import argparse,json,time,hashlib
from pathlib import Path
from datetime import datetime,timezone
from playwright.sync_api import sync_playwright
parser=argparse.ArgumentParser();parser.add_argument('--server',default='http://127.0.0.1:8767');parser.add_argument('--token-file',type=Path,required=True);parser.add_argument('--output',type=Path,default=Path('.ore/reports/ui-v04'));args=parser.parse_args()
output=args.output;output.mkdir(parents=True,exist_ok=True);token=args.token_file.read_text().strip()
base=args.server.rstrip('/');ident=None;errors=[]
report={'kind':'actual_built_ui_with_real_codex_public_response_stream','external_collection_verified':False,'collection_performed':False,'checks':{},'started_at':datetime.now(timezone.utc).isoformat()}
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page();page.on('pageerror',lambda err:errors.append(str(err)))
 try:
  page.goto(base,wait_until='domcontentloaded');page.get_by_label('Operator token').wait_for();page.get_by_label('Operator token').fill(token);page.get_by_role('button',name='Connect workspace').click();page.get_by_label('Message ORE').wait_for()
  prompt='이것은 ORE 대화 UI의 실제 응답 표시를 검증하는 질문이다. 수집 작업이나 실행 계획을 만들지 말고 respond로만 답하라. 웹 자료 수집에서 수집 범위, 공식 목록, 원문 버전, supplementary material, 파일 검증, 누락 보고, 중단과 재개, 재현성을 어떻게 이해해야 하는지 한국어로 8개의 문단으로 설명하라. 각 문단은 3문장으로 작성하라. 도구 호출과 외부 검색은 하지 않는다.'
  page.get_by_label('Message ORE').fill(prompt);started=time.monotonic();page.get_by_role('button',name='Send message').click()
  page.wait_for_url('**/chat/**');ident=page.url.rsplit('/',1)[1];report['conversation_id']=ident
  page.get_by_text('Receiving response…',exact=True).wait_for(timeout=90000)
  page.wait_for_function("[...document.querySelectorAll('[data-message-id] .markdown')].some(node=>node.textContent.length>0)")
  first=page.request.get(base+'/v1/conversations/'+ident).json();public=[m for m in first['messages'] if m['role']=='assistant']
  report['first_public_delta_seconds']=round(time.monotonic()-started,3);report['checks']['actual_partial_message_visible']=bool(public and public[-1]['status']=='streaming' and public[-1]['content'])
  page.screenshot(path=str(output/'live-stream.png'),full_page=True)
  page.get_by_role('button',name='Stop ORE').click()
  page.get_by_role('button',name='Resume',exact=True).wait_for(timeout=45000)
  paused=page.request.get(base+'/v1/conversations/'+ident).json();report['checks']['stop_confirmed_paused']=paused['status']=='paused' and not paused['planner_running']
  partials=[m for m in paused['messages'] if m['role']=='assistant'];saved=partials[-1]['content'];report['saved_partial_chars']=len(saved);report['saved_partial_sha256']=hashlib.sha256(saved.encode()).hexdigest();report['checks']['public_partial_marked_interrupted']=partials[-1]['status']=='interrupted'
  report['checks']['no_execution_run_created']=not paused['runs'];users_before=sum(m['role']=='user' for m in paused['messages'])
  page.reload(wait_until='domcontentloaded');page.get_by_role('button',name='Resume',exact=True).wait_for();page.get_by_text('Response interrupted · received text saved',exact=True).wait_for()
  restored=page.request.get(base+'/v1/conversations/'+ident).json();report['checks']['reload_restores_exact_partial']=next(m for m in restored['messages'] if m['id']==partials[-1]['id'])['content']==saved
  cursor=restored['events_cursor'];page.wait_for_timeout(1700);stable=page.request.get(base+'/v1/conversations/'+ident).json();report['checks']['pause_remains_stable']=stable['status']=='paused' and stable['events_cursor']==cursor
  page.screenshot(path=str(output/'live-stopped-restored.png'),full_page=True)
  page.get_by_role('button',name='Resume',exact=True).click();deadline=time.monotonic()+100
  while time.monotonic()<deadline:
   state=page.request.get(base+'/v1/conversations/'+ident).json()
   if not state['planner_running'] and any(m.get('status')=='complete' and m['role']=='assistant' and m['id']!=partials[-1]['id'] for m in state['messages']):break
   if state['status']=='error':raise AssertionError('Resumed planner returned an error')
   page.wait_for_timeout(350)
  else:raise TimeoutError('Resumed response did not finish')
  report['checks']['resume_preserves_user_message_count']=sum(m['role']=='user' for m in state['messages'])==users_before
  report['checks']['resume_no_workflow_implicitly_created']=not state['runs']
  completed=[m for m in state['messages'] if m['role']=='assistant' and m.get('status')=='complete'];report['completed_response_chars']=len(completed[-1]['content']);report['checks']['resumed_response_complete']=bool(completed[-1]['content'])
  page.get_by_text('Receiving response…',exact=True).wait_for(state='hidden');page.wait_for_timeout(350);page.screenshot(path=str(output/'live-resumed-complete.png'),full_page=True)
  report['checks']['no_browser_page_errors']=not errors;report['ok']=all(report['checks'].values());report['elapsed_seconds']=round(time.monotonic()-started,3)
 except Exception as exc:
  report.update(ok=False,error=type(exc).__name__+': '+str(exc))
  page.screenshot(path=str(output/'live-stream-failure.png'),full_page=True)
  if ident:
   try:page.request.post(base+'/v1/conversations/'+ident+'/interrupt',data={})
   except Exception:pass
 finally:
  report['page_errors']=errors;(output/'live-stream-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');browser.close()
print(json.dumps({k:report[k] for k in ('ok','checks','first_public_delta_seconds','elapsed_seconds','error') if k in report},ensure_ascii=False))
if not report['ok']:raise SystemExit(1)
