"""One bounded visual registration challenge attempt; values use secret references."""
import asyncio,json,time
from pathlib import Path
import httpx
from ore.config import Settings
from ore.store import Store
async def main():
 rows=json.loads(Path('.ore/reports/onboarding-sessions.json').read_text());row=next(x for x in rows if x['name']=='clarivate');sid=row['session']['id'];base=f'/v1/browser/{sid}'
 store=Store(Settings().prepare().database_url);store.initialize()
 attempt=store.reserve_challenge(row['session']['job_id'],'https://access.clarivate.com','institution-onboarding:operator',3,120)
 if not attempt['allowed']:raise RuntimeError('Registration challenge budget exhausted')
 started=time.monotonic()
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=90) as c:
  async def post(path,data):
   r=await c.post(path,json=data);r.raise_for_status();return r.json()
  obs=await post(base+'/observe',{});epoch=obs['epoch']
  for ref,selector in [('onboarding/email','input[name="registrationEmail"]'),('onboarding/password','input[name="password"]'),('onboarding/password','input[name="reEnterPassword"]')]:
   await post(base+'/secret-fill',{'ref':ref,'selector':selector,'epoch':epoch})
  for selector,value in [('input[name="firstName"]','John'),('input[name="lastName"]','Doe'),('input[name="textInput"]','KN9Zu')]:
   await post(base+'/input',{'action':'type','selector':selector,'text':value,'epoch':epoch})
  await post(base+'/input',{'action':'click','selector':'button[type="submit"]:has-text("Register")','epoch':epoch})
  await asyncio.sleep(3);obs=await post(base+'/observe',{})
  result={'name':'clarivate','session_id':sid,'url':obs['url'].split('?')[0],'text':obs['text'][:12000],'elements':obs['elements']}
  Path('.ore/reports/clarivate-registration-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
  resolved=any(x in obs['text'].lower() for x in ('verification email','activation email','check your email','successfully registered','verify your email'))
  store.finish_challenge(attempt['id'],attempt['token'],time.monotonic()-started,resolved,{'registration_page':result['url'],'observed_registration_progress':resolved})
  print(json.dumps(result,ensure_ascii=False))
 store.close()
asyncio.run(main())
