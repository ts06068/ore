import asyncio,json,re
from pathlib import Path
import httpx
from ore.config import Settings,SecretStore
async def main():
 row=next(x for x in json.loads(Path('.ore/reports/onboarding-sessions.json').read_text()) if x['name']=='clarivate');base='/v1/browser/'+row['session']['id'];epoch=row['session']['epoch']
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=120) as c:
  async def post(path,data):
   r=await c.post(path,json=data);r.raise_for_status();return r.json()
  for selector,value in [('input[name="appid"]','ore-yonsei-20260910'),('input[name="appname"]','ORE'),('textarea[name="appdesc"]','Self-hosted research application used by a member of Yonsei University for scholarly metadata search, DOI discovery, citation metadata and authorized retrieval. API credentials are stored server-side. Academic research use; no resale. Local service: http://localhost:8765.')]:
   await post(base+'/input',{'action':'type','selector':selector,'text':value,'epoch':epoch})
  await post(base+'/input',{'action':'click','selector':'select[name="clienttype"]','epoch':epoch})
  for key in ('End','Enter'):await post(base+'/input',{'action':'key','key':key,'epoch':epoch})
  await post(base+'/input',{'action':'click','selector':'button:has-text("Register Application")','epoch':epoch})
  await asyncio.sleep(3)
  await post(base+'/secret-capture',{'selector':'body','ref':'onboarding/clarivate-institution-application-page','epoch':epoch})
  secret=SecretStore(Settings().prepare().state_dir);body=secret.get('onboarding/clarivate-institution-application-page')
  safe=re.sub(r'[A-Za-z0-9._~+/=\-]{24,}','[private identifier]',body)
  email=secret.get('onboarding/institution-email')
  if email:safe=safe.replace(email,'[private account]')
  result={'application_id':'ore-yonsei-20260910','text':safe[:13000]};Path('.ore/reports/clarivate-institution-application-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
asyncio.run(main())
