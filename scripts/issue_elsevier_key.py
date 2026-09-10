"""User-approved API-key application and private capture; no key values are displayed."""
import asyncio,json,re
from pathlib import Path
import httpx
from ore.config import Settings,SecretStore
async def main():
 row=next(x for x in json.loads(Path('.ore/reports/onboarding-sessions.json').read_text()) if x['name']=='elsevier');base='/v1/browser/'+row['session']['id'];epoch=row['session']['epoch']
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=120) as c:
  async def post(path,data):
   r=await c.post(path,json=data);r.raise_for_status();return r.json()
  await post(base+'/input',{'action':'click','selector':'text=I agree with the API Service Agreement','epoch':epoch})
  await post(base+'/input',{'action':'click','selector':'button[name="register"]','epoch':epoch})
  await asyncio.sleep(4)
  await post(base+'/secret-capture',{'ref':'onboarding/elsevier-key-page','selector':'body','epoch':epoch})
  secrets=SecretStore(Settings().prepare().state_dir);page=secrets.get('onboarding/elsevier-key-page')
  matches=list(dict.fromkeys(re.findall(r'\b[a-fA-F0-9]{32}\b',page)))
  if len(matches)!=1:
   print(json.dumps({'key_created':False,'status':'requires_private_page_inspection','candidate_count':len(matches)}));return
  secrets.set('institution/scopus',matches[0])
  profiles=(await c.get('/v1/access-profiles')).json();profile=next(x for x in profiles if x['id']=='institution-onboarding')
  profile.setdefault('sources',{})['scopus']={'api_key_ref':'institution/scopus'}
  await post('/v1/access-profiles',profile)
  visible=page.replace(matches[0],'[private API key]')
  email=secrets.get('onboarding/email')
  if email:visible=visible.replace(email,'[private account]')
  result={'key_created':True,'secret_ref':'institution/scopus','tdm_optional_agreement_accepted':False,'label':'ORE','website':'http://localhost:8765','page':visible[:12000]}
  Path('.ore/reports/elsevier-key-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
asyncio.run(main())
