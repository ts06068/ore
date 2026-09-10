"""One-time user-authorized WoS member-plan request; captures any key privately."""
import asyncio,json,re
from pathlib import Path
from datetime import datetime,timezone
import httpx
from ore.config import Settings,SecretStore

async def main():
 row=next(x for x in json.loads(Path('.ore/reports/onboarding-sessions.json').read_text()) if x['name']=='clarivate')
 base='/v1/browser/'+row['session']['id'];epoch=row['session']['epoch'];secrets=SecretStore(Settings().prepare().state_dir)
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=120) as c:
  for data in [{'action':'click','selector':'tr:has-text("Free Institutional Member Plan") input[name="plan"]'}, {'action':'click','selector':'button:has-text("Subscribe!")'}]:
   response=await c.post(base+'/input',json={**data,'epoch':epoch});response.raise_for_status()
  await asyncio.sleep(4)
  response=await c.post(base+'/secret-capture',json={'ref':'onboarding/clarivate-institution-subscription-page','selector':'body','epoch':epoch});response.raise_for_status()
  raw=secrets.get('onboarding/clarivate-institution-subscription-page')
  for ref in ['onboarding/email','onboarding/institution-email']:
   value=secrets.get(ref)
   if value:raw=raw.replace(value,'[private account]')
  result={'time':datetime.now(timezone.utc).isoformat(),'application_id':'ore-yonsei-20260910','requested_plan':'Free Institutional Member Plan','text':re.sub(r'[A-Za-z0-9._~+/=\-]{24,}','[private identifier]',raw)[:16000]}
  Path('.ore/reports/clarivate-institution-subscription-result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
asyncio.run(main())
