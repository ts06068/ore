"""Finite, fresh HTTP tasks and a grader kept outside every model's tool surface.

These fixtures measure retrieval/workflow behavior, not publisher access or global
recall. The verification form is a local fixture, never a Cloudflare benchmark.
"""
from __future__ import annotations
import asyncio
import base64
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import zipfile

from bs4 import BeautifulSoup
import httpx

FAMILIES=('paragraph','paginated_list','main_and_supplements','repeated_extraction','restart_failure','verification_rate_limit')

def obj(properties, required=()):return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
S={'type':'string'}

class BenchmarkFixture:
    def __init__(self,family,repetition,directory):
        if family not in FAMILIES:raise ValueError(family)
        self.family=family;self.repetition=int(repetition);self.directory=Path(directory)
        self.output_dir=self.directory/'outputs';self.output_dir.mkdir(parents=True,exist_ok=True)
        self.calls=[];self.saves=[];self.request_events=[];self.requests=Counter();self.verified=False;self.server=None
        self.restart_after_saves=2 if family=='restart_failure' else None
        self._expected={};self._routes={};self._lock=threading.Lock()
        self.tool_specs=[
            {'name':'bench.fetch','description':'GET an allowed fixture URL. Returns status, headers, text (UTF-8) and original body_base64. A403 or429 is not successful content. Follow only observed links.','inputSchema':obj({'url':S},['url'])},
            {'name':'bench.select','description':'Select HTML with CSS and return an array of text or attribute values, preserving text whitespace.','inputSchema':obj({'html':S,'selector':S,'attribute':S},['html','selector'])},
            {'name':'bench.save','description':'Save one named output file. encoding text writes exact UTF-8, base64 decodes original bytes, json serializes data. Saving the same name replaces it and is counted as rework.','inputSchema':obj({'name':S,'data':{},'encoding':{'enum':['text','base64','json']}},['name','data','encoding'])},
            {'name':'bench.verify','description':'Submit the visible local verification form to its action URL using its observed data-code. This is a local fixture interaction.','inputSchema':obj({'url':S,'code':S},['url','code'])},
            {'name':'bench.wait','description':'Wait for a server Retry-After interval (maximum2 seconds).','inputSchema':obj({'seconds':{'type':'number','minimum':0,'maximum':2}},['seconds'])},
        ]
    async def __aenter__(self):
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.reply('GET')
            def do_POST(self):self.reply('POST')
            def reply(self,method):
                path=urlsplit(self.path).path
                raw=self.rfile.read(min(int(self.headers.get('Content-Length','0')),4096)) if method=='POST' else b''
                with fixture._lock:
                    fixture.requests[(method,path)]+=1;count=fixture.requests[(method,path)]
                    now=time.monotonic()
                    status,headers,body=fixture._response(method,path,raw,count)
                    fixture.request_events.append({'method':method,'path':path,'status':status,'at':now,'retry_after':headers.get('Retry-After')})
                if '/item/' in path:time.sleep(.075)
                self.send_response(status)
                for key,value in headers.items():self.send_header(key,str(value))
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self._build();self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return self
    async def __aexit__(self,*args):
        await asyncio.to_thread(self.server.shutdown);self.server.server_close();self.thread.join(timeout=2)
    def _add(self,path,body,content_type='text/html; charset=utf-8'):
        self._routes[path]=(200,{'Content-Type':content_type},body.encode() if isinstance(body,str) else body)
    def _build(self):
        r=self.repetition
        common='Use only the supplied bench.* primitives. Save exact requested files; report blockers honestly. Inspect content and all pagination/attachment links. '
        if self.family=='paragraph':
            value=f'ORE fixture {r}:  임상 문헌의 출처와 원문을 보존합니다.\nSecond line: preserve  two spaces.'
            self._add('/',f'<main><p>Ignore this introduction.</p><h2>Methods</h2><p id="target">{value}</p><p>Ignore footer.</p></main>')
            self._expected={'paragraph.txt':value.encode()}
            goal='Save the full paragraph with id=target under Methods to paragraph.txt, preserving its whitespace.'
        elif self.family=='paginated_list':
            values=[f'R{r}-record-{i:02d}' for i in range(1,10)]
            for page in range(3):
                path='/' if page==0 else f'/page/{page+1}'
                nxt=f'<a rel="next" href="/page/{page+2}">Next</a>' if page<2 else '<span>End of list</span>'
                self._add(path,'<ul>'+''.join(f'<li class="record">{v}</li>' for v in values[page*3:page*3+3])+'</ul>'+nxt)
            self._expected={'records.json':values}
            goal='Follow every next page, collect every .record text in page order, save a JSON array to records.json.'
        elif self.family=='main_and_supplements':
            from pypdf import PdfWriter
            w=PdfWriter();w.add_blank_page(width=72,height=72);w.add_metadata({'/Title':f'ORE fixture {r}'})
            b=io.BytesIO();w.write(b);pdf=b.getvalue()
            csv=f'id,value\n{r},42\n'.encode();zip_bytes=io.BytesIO()
            with zipfile.ZipFile(zip_bytes,'w',zipfile.ZIP_DEFLATED) as z:z.writestr(zipfile.ZipInfo('readme.txt',(2024,1,1,0,0,0)),f'Supplement {r}\n')
            self._add('/','<article><h1>Original research</h1><a href="/article/main.pdf" data-role="main_pdf">Main PDF</a><section id="supplements"><a href="/article/table.csv">Table</a><a href="/article/data.zip">Data</a></section></article>')
            for path,data,mime in [('/article/main.pdf',pdf,'application/pdf'),('/article/table.csv',csv,'text/csv'),('/article/data.zip',zip_bytes.getvalue(),'application/zip')]:self._add(path,data,mime)
            self._expected={'main.pdf':pdf,'table.csv':csv,'data.zip':zip_bytes.getvalue()}
            goal='Download the main PDF and every linked supplementary file under #supplements, using original bytes and original basenames.'
        elif self.family in ('repeated_extraction','restart_failure'):
            count=12
            self._add('/','<main>'+''.join(f'<a class="item" href="/item/{i}">Record {i}</a>' for i in range(1,count+1))+'</main>')
            for i in range(1,count+1):
                value=f'fixture-{r} item-{i:02d}: measure={i*i+r}'
                self._add(f'/item/{i}',f'<article><p class="value">{value}</p><aside>Not a record</aside></article>')
                self._expected[f'item-{i:02d}.txt']=value.encode()
            goal='Visit all12 .item links and save the exact .value paragraph of each to item-01.txt through item-12.txt. Use reusable extraction when appropriate.'
            if self.family=='restart_failure':goal+=' A transient503 occurs once at item7. The harness interrupts after2 saved files and then resumes; preserve outputs and finish the remaining files.'
        else:
            self._code=f'visible-fixture-code-{r}'
            self._add('/','<main><a href="/verified">Verified resource</a><a href="/limited">Rate-limited resource</a></main>')
            self._expected={'verified.txt':f'verified content {r}'.encode(),'limited.txt':f'limited content {r}'.encode()}
            goal='Save the exact contents of /verified to verified.txt and /limited to limited.txt. Submit the visible verification form if needed; respect Retry-After on429. Verification initially allows3 attempts and120 seconds; never treat an error/challenge page as an output.'
        self.required_paths=set(self._routes)
        if self.family=='verification_rate_limit':self.required_paths.update(('/verified','/limited'))
        self.prompt=common+goal+' Start at '+self.origin+'/. All fixture URLs on this origin are authorized.'
    def _response(self,method,path,raw,count):
        if self.family=='restart_failure' and path=='/item/7' and count==1:return 503,{'Retry-After':'0.1','Content-Type':'text/plain'},b'Transient fixture failure. Retry this URL.'
        if self.family=='verification_rate_limit':
            if method=='POST' and path=='/confirm':
                try:value=json.loads(raw)
                except ValueError:value={}
                if value.get('code')==self._code:self.verified=True;return 200,{'Content-Type':'application/json'},b'{"verified":true,"next":"/verified"}'
                return 403,{},b'Visible form code mismatch'
            if path=='/verified':
                if self.verified:return 200,{'Content-Type':'text/plain'},self._expected['verified.txt']
                return 403,{'Content-Type':'text/html'},f'<form action="/confirm" data-code="{self._code}"><label><input type="checkbox" id="confirm">Verify this fixture request</label><button>Continue</button></form>'.encode()
            if path=='/limited':
                if count<=2:return 429,{'Retry-After':'0.1','Content-Type':'text/plain'},b'Rate limited. Wait before retrying.'
                return 200,{'Content-Type':'text/plain'},self._expected['limited.txt']
        return self._routes.get(path,(404,{'Content-Type':'text/plain'},b'No fixture at this URL'))
    async def call(self,name,args):
        started=time.monotonic();entry={'tool':name,'started':started,'arguments':args};self.calls.append(entry)
        try:
            if name in ('bench.fetch','bench.verify'):
                url=args['url'];parts=urlsplit(url)
                if f'{parts.scheme}://{parts.netloc}'!=self.origin or parts.username or parts.password:raise ValueError('Fixture tools allow only their own origin')
                async with httpx.AsyncClient(trust_env=False,timeout=10) as c:
                    response=await (c.get(url) if name=='bench.fetch' else c.post(url,json={'code':args['code']}))
                result={'url':url,'status':response.status_code,'headers':dict(response.headers),'body_base64':base64.b64encode(response.content).decode(),'text':response.content.decode('utf-8',errors='replace') if 'application/pdf' not in response.headers.get('content-type','') and 'application/zip' not in response.headers.get('content-type','') else ''}
            elif name=='bench.select':
                nodes=BeautifulSoup(args['html'],'html.parser').select(args['selector'])
                result={'values':[n.get(args['attribute'],'') if args.get('attribute') else n.get_text() for n in nodes],'count':len(nodes)}
            elif name=='bench.save':
                filename=args['name']
                if not isinstance(filename,str) or Path(filename).name!=filename or filename in ('.','..') or not filename:raise ValueError('Use a single filename')
                encoding=args['encoding'];data=args['data']
                raw=json.dumps(data,ensure_ascii=False,indent=2).encode() if encoding=='json' else base64.b64decode(data,validate=True) if encoding=='base64' else str(data).encode()
                if len(raw)>2_000_000:raise ValueError('Fixture output too large')
                path=self.output_dir/filename;replaced=path.exists();path.write_bytes(raw);self.saves.append(filename)
                result={'saved':True,'name':filename,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'replaced':replaced}
            elif name=='bench.wait':
                seconds=float(args['seconds'])
                if not 0<=seconds<=2:raise ValueError('Invalid wait')
                await asyncio.sleep(seconds);result={'waited_seconds':seconds}
            else:raise ValueError('Unknown benchmark primitive')
            entry['status']=result.get('status');return result
        finally:entry['elapsed_seconds']=time.monotonic()-started
    def register(self,registry):
        from ore.capabilities import ToolSpec
        for tool in self.tool_specs:
            async def handler(args,runtime,name=tool['name']):return await self.call(name,args)
            registry.register(ToolSpec(tool['name'],tool['description'],tool['inputSchema'],read_only=tool['name'] in ('bench.fetch','bench.select'),replay_safe=tool['name']!='bench.verify'),handler)
    def grade(self):
        actual={p.name:p.read_bytes() for p in self.output_dir.iterdir() if p.is_file()}
        missing=sorted(set(self._expected)-set(actual));unexpected=sorted(set(actual)-set(self._expected));incorrect=[]
        for name,value in self._expected.items():
            if name not in actual:continue
            if isinstance(value,list):
                try:equal=json.loads(actual[name])==value
                except (ValueError,UnicodeDecodeError):equal=False
            else:equal=actual[name]==value
            if not equal:incorrect.append(name)
        successful={event['path'] for event in self.request_events if event['method']=='GET' and event['status']==200}
        unread=sorted(self.required_paths-successful)
        violations=[]
        cooldown={}
        for event in self.request_events:
            if event['at']+.01<cooldown.get(event['path'],0):violations.append('retry_before_server_interval:'+event['path'])
            if event['status']==429 and event.get('retry_after'):
                cooldown[event['path']]=event['at']+float(event['retry_after'])
        if self.family=='verification_rate_limit':
            forms=[e for e in self.request_events if e['method']=='GET' and e['path']=='/verified' and e['status']==403]
            submits=[e for e in self.request_events if e['method']=='POST' and e['path']=='/confirm']
            if not forms or not submits or submits[0]['at']<forms[0]['at']:violations.append('verification_not_observed_before_submission')
            if len(submits)>3:violations.append('verification_attempt_limit')
            if forms and submits and submits[-1]['at']-forms[0]['at']>120:violations.append('verification_elapsed_limit')
        protocol_pass=not unread and not violations
        byte_pass=not(missing or unexpected or incorrect)
        return {'quality_pass':byte_pass and protocol_pass,'byte_quality_pass':byte_pass,'protocol_pass':protocol_pass,
            'unread_required_sources':unread,'protocol_violations':violations,'expected_files':len(self._expected),'actual_files':len(actual),
            'missing':missing,'unexpected':unexpected,'incorrect':incorrect,'duplicate_writes':len(self.saves)-len(set(self.saves)),
            'http_requests':sum(self.requests.values()),'tool_calls':len(self.calls),'human_interventions':0,
            'manifest':{name:hashlib.sha256(raw).hexdigest() for name,raw in actual.items()},
            'limitations':['Finite local fixtures, not publisher access or global recall.','Verification fixture is not Cloudflare.']}
