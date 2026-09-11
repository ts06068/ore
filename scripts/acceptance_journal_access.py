"""Read-only official-TOC access probes; not article or supplement acceptance."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
from ore.policy import AccessPolicy, GuardedTransport, RateLimiter, redact
from ore.store import Store

URLS={
 'jacc':'https://www.jacc.org/toc/jacc/83/1',
 'ehj':'https://academic.oup.com/eurheartj/issue/45/1',
 'circulation':'https://www.ahajournals.org/toc/circ/149/1',
 'jama-cardiology':'https://jamanetwork.com/journals/jamacardiology/issue/9/1',
}
async def run(args):
    state=args.state_dir.resolve();profiles=json.loads((state/'access-profiles.json').read_text())
    profile=next(p for p in profiles if p['id']==args.profile)
    store=Store('sqlite:///'+str(state/'ore.db'));store.initialize()
    limiter=RateLimiter(store);started=datetime.now(timezone.utc).isoformat()
    output=args.report.parent/'official-toc-v04';output.mkdir(parents=True,exist_ok=True)
    async def probe(name,url):
        origin=urlsplit(url);mission={'goal':'Read one authorized official TOC for access diagnostics','allowed_origins':[f'{origin.scheme}://{origin.netloc}'],'sources':[]}
        transport=GuardedTransport(AccessPolicy(mission,profile,operation='browser'),limiter,3,profile['id'])
        result={'journal':name,'url':url,'inventory_status':'unverified','downloaded_article_files':0}
        try:
            async with asyncio.timeout(35),httpx.AsyncClient(transport=transport,timeout=20,follow_redirects=True) as client:
                async with client.stream('GET',url) as response:
                    chunks=[];size=0
                    async for data in response.aiter_bytes():
                        size+=len(data)
                        if size>8_000_000:raise ValueError('Diagnostic response limit')
                        chunks.append(data)
                    body=b''.join(chunks);text=body.decode('utf-8',errors='replace')
                    challenge=any(t in text.casefold() for t in ('just a moment','verify you are human','challenge-platform','checking your browser'))
                    (output/(name+'.html')).write_bytes(body)
                    result.update(status=response.status_code,final_url=redact(str(response.url)),bytes=len(body),sha256=hashlib.sha256(body).hexdigest(),
                        challenge_marker_observed=challenge,access_state='challenge_or_block' if challenge or response.status_code in (401,403,429) else 'response_observed',
                        title=re.sub('<[^>]+>','',m.group(1))[:200] if (m:=re.search(r'<title[^>]*>(.*?)</title>',text,re.I|re.S)) else None)
        except Exception as exc:result.update(access_state='probe_failed',error=type(exc).__name__)
        return result
    try:rows=await asyncio.gather(*(probe(name,url) for name,url in URLS.items()))
    finally:store.close()
    value={'schema_version':'ore.official-access-probe/v1','started_at':started,'finished_at':datetime.now(timezone.utc).isoformat(),'journals':rows,
        'whole_issue_collection_verified':False,'inventory_sealed':False,'supplement_manifest_verified':False,'model_calls':0,'existing_jobs_modified':False}
    args.report.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'report':str(args.report),'journals':[{k:r.get(k) for k in ('journal','status','access_state')} for r in rows]}))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state-dir',type=Path,required=True);p.add_argument('--profile',required=True);p.add_argument('--report',type=Path,default=Path('.ore/reports/official-journal-access-v04.json'))
    asyncio.run(run(p.parse_args()))
