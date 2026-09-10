import asyncio,json,re
from pathlib import Path
import httpx
from ore.config import Settings,SecretStore
async def main():
 row=next(x for x in json.loads(Path('.ore/reports/onboarding-sessions.json').read_text()) if x['name']=='clarivate');base='/v1/browser/'+row['session']['id'];epoch=row['session']['epoch']
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=120) as c:
  async def post(path,data):
   r=await c.post(path,json=data);r.raise_for_status();return r.json()
  await post(base+'/input',{'action':'navigate','url':'https://developer.clarivate.com/apis/wos-starter','epoch':epoch})
  await asyncio.sleep(2);d=await post(base+'/observe',{})
  safe=re.sub(r'[A-Za-z0-9._~+/=\-]{32,}','[private identifier]',d['text'])
  result={'url':d['url'].split('?')[0],'text':safe[:14000],'elements':d['elements']};Path('.ore/reports/clarivate-subscription-options.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
asyncio.run(main())
