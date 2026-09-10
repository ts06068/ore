import asyncio,json
from pathlib import Path
import httpx
async def main():
 rows=json.loads(Path('.ore/reports/onboarding-sessions.json').read_text())
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=120) as c:
  async def post(path,data):
   r=await c.post(path,json=data);r.raise_for_status();return r.json()
  row=next(x for x in rows if x['name']=='elsevier');base='/v1/browser/'+row['session']['id'];epoch=row['session']['epoch']
  for selector,value in [('input[name="projectName"]','ORE'),('input[name="websiteURL"]','http://localhost:8765')]:
   await post(base+'/input',{'action':'type','selector':selector,'text':value,'epoch':epoch})
  row=next(x for x in rows if x['name']=='clarivate');base='/v1/browser/'+row['session']['id'];epoch=row['session']['epoch']
  await post(base+'/input',{'action':'navigate','url':'https://developer.clarivate.com/login','epoch':epoch})
  await asyncio.sleep(2)
  for ref,selector in [('onboarding/email','input[name="email"]'),('onboarding/password','input[name="password"]')]:
   await post(base+'/secret-fill',{'ref':ref,'selector':selector,'epoch':epoch})
  await post(base+'/input',{'action':'click','selector':'button[name="login-btn"]','epoch':epoch})
  await asyncio.sleep(3);d=await post(base+'/observe',{})
  result={'url':d['url'].split('?')[0],'text':d['text'][:14000],'elements':d['elements']}
  Path('.ore/reports/clarivate-activated-state.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
asyncio.run(main())
