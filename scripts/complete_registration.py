"""Continue the explicitly authorized account signup; never print secret values."""
import asyncio,json
from pathlib import Path
import httpx
async def main():
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=120) as c:
  async def post(path,data):
   r=await c.post(path,json=data);r.raise_for_status();return r.json()
  rows=json.loads(Path('.ore/reports/onboarding-sessions.json').read_text())
  row=next(x for x in rows if x['name']=='elsevier');sid=row['session']['id'];base=f'/v1/browser/{sid}'
  obs=await post(base+'/observe',{});epoch=obs['epoch']
  for selector,text in [('input[name="givenName"]','John'),('input[name="familyName"]','Doe')]:
   await post(base+'/input',{'action':'type','selector':selector,'text':text,'epoch':epoch})
  await post(base+'/secret-fill',{'ref':'onboarding/password','selector':'input[name="pf.pass"]','epoch':epoch})
  await post(base+'/input',{'action':'click','selector':'button[name="registerId"]','epoch':epoch})
  await asyncio.sleep(3);obs=await post(base+'/observe',{})
  result={'name':'elsevier','session_id':sid,'url':obs['url'].split('?')[0],'text':obs['text'][:10000],'elements':obs['elements'],'tabs':obs['tabs']}
  Path('.ore/reports/elsevier-registration-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
asyncio.run(main())
