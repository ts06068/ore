#!/usr/bin/env python3
"""Actual Astra/high collection of a whole local issue through normal Engine tools.

The independently computed expectations are retained by this process and are never
put in the mission, Rune, model prompt, or served HTTP routes. This establishes a
controlled fixture workflow, not access to or completeness of any real journal.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import threading
import time
import zipfile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from ore.config import Settings
from ore.engine import Engine
from ore.evaluation import runtime_fingerprint

DOI = '10.9999/ore.fixture.study'
EDITORIAL_DOI = '10.9999/ore.fixture.editorial'
TITLE = 'Field measurements in the controlled observatory cohort'


def digest(data):return hashlib.sha256(data).hexdigest()
def utc():return datetime.now(timezone.utc).isoformat()


def pdf_bytes():
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    font_ref=writer._add_object(font)
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font_ref})})
    stream=DecodedStreamObject()
    content=f'BT /F1 12 Tf 40 740 Td ({TITLE}) Tj 0 -25 Td (DOI: {DOI}) Tj 0 -25 Td (Original Article - final published version) Tj 0 -25 Td (Twenty-four observations were recorded at three stations.) Tj ET'
    stream.set_data(content.encode('ascii'));page[NameObject('/Contents')]=writer._add_object(stream)
    writer.add_metadata({'/Title':TITLE,'/Producer':'ORE deterministic acceptance fixture'})
    output=BytesIO();writer.write(output);body=output.getvalue()
    assert DOI in PdfReader(BytesIO(body)).pages[0].extract_text()
    return body


def fixture(origin):
    study=f'/doi/{DOI}';editorial=f'/doi/{EDITORIAL_DOI}'
    csv=b'station,observations,mean_delay_ms\nA,8,2.5\nB,8,3.0\nC,8,2.75\n'
    archive=BytesIO()
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as stream:
        info=zipfile.ZipInfo('protocol.txt',(2024,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
        stream.writestr(info,'This protocol describes the controlled observatory measurements.\n')
    documents={
        '/issue/2024/1':('text/html; charset=utf-8',f'''<!doctype html><html><head><title>Observatory Research, Volume 1 Issue 1, January 2024</title></head><body>
        <h1>Volume 1, Issue 1 — January 2024</h1><p>This regular issue is the complete published table of contents.</p>
        <main class="issue-toc" data-total-articles="2">
        <article class="issue-item"><p class="article-type">Original Article</p><h2><a class="article-title" href="{study}">{TITLE}</a></h2></article>
        <article class="issue-item"><p class="article-type">Editorial</p><h2><a class="article-title" href="{editorial}">Editorial perspective on measurement</a></h2></article>
        </main><footer data-issue-complete="true">End of this issue</footer></body></html>'''.encode()),
        study:('text/html; charset=utf-8',f'''<!doctype html><html><head><title>{TITLE}</title><meta name="citation_doi" content="{DOI}"><meta name="citation_pdf_url" content="{origin}/assets/study.pdf"></head><body>
        <h1>{TITLE}</h1><p class="article-type">Original Article</p><p>DOI: {DOI}</p><p class="publication-version">Published version of record, 2 January 2024</p>
        <p>Original research reporting field measurements from twenty-four observations.</p><a class="main-pdf" href="/assets/study.pdf">Download published article PDF</a>
        <section class="supplementary-material" data-supplement-count="2" data-attachments-complete="true"><h2>Supplementary material</h2>
        <a href="/assets/observations.csv">Supplement 1: observed measurements (CSV)</a>
        <a href="/assets/protocol.zip">Supplement 2: full measurement protocol (ZIP)</a></section><footer>End of complete supplementary listing.</footer></body></html>'''.encode()),
        editorial:('text/html; charset=utf-8',f'''<!doctype html><html><head><title>Editorial perspective on measurement</title><meta name="citation_doi" content="{EDITORIAL_DOI}"></head><body><h1>Editorial perspective on measurement</h1><p class="article-type">Editorial</p><p>This editorial is commentary on the issue.</p><p data-supplement-count="0">No supplementary material.</p></body></html>'''.encode()),
        '/assets/study.pdf':('application/pdf',pdf_bytes()),
        '/assets/observations.csv':('text/csv',csv),
        '/assets/protocol.zip':('application/zip',archive.getvalue()),
    }
    expected={'article_classifications':{'doi:'+DOI:'included','doi:'+EDITORIAL_DOI:'excluded'},
              'assets':[{'url':origin+path,'role':'main_pdf' if path.endswith('.pdf') else 'supplement','sha256':digest(body),'bytes':len(body)} for path,(_,body) in documents.items() if path.startswith('/assets/')],
              'fixture_document_hashes':{path:digest(body) for path,(_,body) in documents.items()}}
    return documents,expected


def profiles():
    base={'origins':['127.0.0.1'],'authority':'journal','journal_is_publisher':True,
          'include_labels':['Original Article'],'exclude_labels':['Editorial'],'article_type_selector':'.article-type',
          'verification':'reviewed_deterministic_fixture_only'}
    return [{**base,'id':'ore-fixture.issue.v1','kind':'html_issue','journal_id':'controlled-observatory-fixture',
        'container_selector':'.issue-toc','row_selector':'.issue-item','link_selector':'a.article-title','complete_selector':'footer[data-issue-complete="true"]',
        'next_selector':'a[rel="next"]','pending_selector':'[aria-busy="true"]','article_url_pattern':r'/doi/',
        'count_selector':'[data-total-articles]','count_attribute':'data-total-articles'},
        {**base,'id':'ore-fixture.article.v1','kind':'html_article','main_selector':'meta[name="citation_pdf_url"], a.main-pdf',
         'supplement_selector':'.supplementary-material a[href]','attachments_complete_selector':'[data-attachments-complete="true"]',
         'empty_supplements_selector':'[data-supplement-count="0"]','pending_selector':'[aria-busy="true"]','final_version_from_official_pdf_link':True}]


async def run(root,timeout):
    root=Path(root).resolve();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
    state=root/stamp
    shared=Path('/workspace/ore/.ore')
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(shared/'browsers')
    os.environ['LD_LIBRARY_PATH']=str(shared/'browser-libs/usr/lib/x86_64-linux-gnu')
    requests=[];documents={}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({'path':self.path,'at':utc()})
            item=documents.get(self.path)
            if item is None:self.send_error(404);return
            media,body=item;self.send_response(200);self.send_header('Content-Type',media)
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);origin=f'http://127.0.0.1:{server.server_port}'
    documents,expected=fixture(origin);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    engine=Engine(Settings(state_dir=state,database_url=f'sqlite:///{state/"ore.db"}',auth_token=secrets.token_urlsafe(32),max_workers=1,execution_backend='local'))
    engine.save_profile({'id':'fixture-only','name':'Controlled public local issue','allowed_hosts':['127.0.0.1'],'origins':[origin],
                         'persist_session':False,'api_interval':0,'max_browser_sessions':2})
    for profile in profiles():engine.coverage.register_profile(profile,reviewer='controlled-fixture-author')
    issue_url=origin+'/issue/2024/1'
    rune={'protocol_id':'ore.acceptance.whole-issue-fixture','protocol_version':'1.0.0',
          'instructions':'Collect the entire explicitly targeted issue. Use its authoritative article type to include original research and exclude other article types. Preserve the final published main PDF and every declared supplementary file in its original format. Use the trusted issue and article profile IDs available through state to seal observed snapshot evidence. Register resource identity from the sealed article_key. Follow the normal ORE workflow and finish only after checking the audit. Do not delegate.',
          'scope':{'collection_kind':'journal','coverage_schema':'ore.coverage/v1','artifact_roles':['main_pdf','supplement']},
          'checks':['Sealed complete issue inventory','Sealed article attachment inventories','No missing required original bytes']}
    mission={'goal':f'Collect all original research articles and their final published PDF plus every supplementary material from the whole regular issue at {issue_url}. Exclude editorials and other non-original article types. Use the normal ORE evidence and audit workflow.',
             'urls':[issue_url],'scope':{'issue_urls':[issue_url],'article_versions':['published_version']},'sources':[],
             'allowed_origins':[origin],'artifact_roles':['main_pdf','supplement'],'completeness':'systematic','access_profile':'fixture-only',
             'model_policy':'fixed','model':'gpt-6-astra','effort':'high','budget':{'max_turns':35,'max_seconds':timeout,'max_agent_workers':1,'max_bytes':10_000_000},
             'limits':{'origin_min_interval_seconds':0},'external_model_content':'selected_page_content'}
    before=runtime_fingerprint();started=time.monotonic();job=engine.create(mission,rune);job_id=job['id'];last_count=-1
    print(json.dumps({'event':'fixture_started','job_id':job_id,'state_dir':str(state),'target':issue_url,'model':'gpt-6-astra','effort':'high'}),flush=True)
    try:
        await engine.run(job_id)
        while time.monotonic()-started<timeout+30:
            current=engine.store.get_job(job_id);events=engine.store.events(job_id)
            decisions=[e for e in events if e['type']=='agent_decision']
            if len(decisions)!=last_count:
                last_count=len(decisions)
                print(json.dumps({'event':'progress','elapsed_seconds':round(time.monotonic()-started,1),'status':current['status'],'model_decisions':last_count,
                                  'last_tool':decisions[-1]['payload']['decision']['tool'] if decisions else None}),flush=True)
            if current['status'] not in ('queued','running'):break
            await asyncio.sleep(0.5)
        else:await engine.pause(job_id,status='paused_budget')
        current=engine.store.get_job(job_id);events=engine.store.events(job_id);artifacts=engine.store.artifacts(job_id)
        issues=engine.coverage._current(job_id,'issue');articles=engine.coverage._current(job_id,'article')
        audit=engine.audit(job_id);errors=[]
        observed_classifications={r['article_key']:r['classification'] for issue in issues for r in issue['entries']}
        if observed_classifications!=expected['article_classifications']:errors.append('Issue identity/classification manifest differs from hidden expectation')
        actual=[]
        for artifact in artifacts:
            path=Path(artifact['path']);actual.append({'url':artifact.get('requested_source_url') or artifact.get('source_url'), 'role':artifact['role'],
                'sha256':digest(path.read_bytes()),'bytes':path.stat().st_size,'status':artifact['status'],'integrity':artifact['integrity'],
                'identity':artifact.get('identity'),'identity_evidence':artifact.get('identity_evidence'),'media_type':artifact['media_type'],'version':artifact.get('version'),
                'resource_id':artifact.get('resource_id'),'artifact_id':artifact['id'],'path':str(path)})
        minimal=lambda rows:sorted((x['url'],x['role'],x['sha256'],x['bytes']) for x in rows)
        if minimal(actual)!=minimal(expected['assets']):errors.append('Downloaded original files differ from hidden role/URL/byte/hash expectation')
        if any(a['status']!='verified' or a['integrity']!='verified' for a in actual):errors.append('At least one actual artifact failed verification')
        if any(a['role']=='main_pdf' and (a['version']!='published_version' or not a['identity_evidence'].get('doi_match')) for a in actual):errors.append('Main final-version DOI validation failed')
        selections=[e['payload'] for e in events if e['type']=='model_selected'];decisions=[e['payload'] for e in events if e['type']=='agent_decision']
        if not decisions or any(s.get('model')!='gpt-6-astra' or s.get('effort')!='high' for s in selections):errors.append('Actual Astra/high decisions were not verified')
        if current['status']!='completed' or audit['status']!='complete_within_scope':errors.append('Normal Engine completion audit did not pass')
        report={'schema_version':'ore.acceptance-fixture/v1','mode':'real_astra_high_normal_engine_whole_issue_fixture','finished_at':utc(),'job_id':job_id,
                'state_dir':str(state),'scope':'One controlled local regular issue, not a real journal','hidden_expectations_in_prompt':False,
                'manual_tool_driving':False,'scripted_model_decisions':False,'passed':not errors,'errors':errors,'job_status':current['status'],
                'elapsed_seconds':round(time.monotonic()-started,3),'model_decisions':len(decisions),'model_selections':selections,
                'observed_usage':[d.get('usage',{}) for d in decisions],'tool_sequence':[d['decision']['tool'] for d in decisions],
                'audit':audit,'expected_manifest':expected,'observed_classifications':observed_classifications,'artifacts':actual,
                'sealed_issues':issues,'sealed_articles':articles,'http_requests':requests,
                'runtime_before':before,'runtime_after':runtime_fingerprint(),'runtime_changed_during_run':before!=runtime_fingerprint(),
                'events':events,'mission':current['mission'],'rune':current['rune']}
        report_path=state/'reports'/'whole-issue-fixture.json';report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n')
        pointer=root/'latest-report.json';pointer.write_text(json.dumps({'path':str(report_path),'sha256':digest(report_path.read_bytes()),'passed':report['passed']},indent=2)+'\n')
        print(json.dumps({'event':'fixture_finished','passed':report['passed'],'errors':errors,'status':current['status'],'model_decisions':len(decisions),
                          'elapsed_seconds':report['elapsed_seconds'],'report_path':str(report_path),'report_sha256':digest(report_path.read_bytes()),'audit':audit}),flush=True)
        return 0 if report['passed'] else 1
    finally:
        await engine.stop();server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-root',default='.ore/acceptance-scholarly-v02');parser.add_argument('--timeout',type=int,default=240)
    args=parser.parse_args();raise SystemExit(asyncio.run(run(args.state_root,args.timeout)))
