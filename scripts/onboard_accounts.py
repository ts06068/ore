"""Open authorized account onboarding in the operator console; secrets use refs only."""
import asyncio,json
from pathlib import Path
import httpx
async def main():
 token=Path('.ore/operator.token').read_text().strip()
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+token},timeout=180) as c:
  async def post(path,data):
   r=await c.post(path,json=data);r.raise_for_status();return r.json()
  profile={'id':'institution-onboarding','name':'Institution access onboarding','persist_browser_profile':True,'principal_id':'operator','max_browser_sessions':8,'sources':{}}
  await post('/v1/access-profiles',profile)
  sessions=[]
  for name,url in [('elsevier','https://dev.elsevier.com/apikey/manage'),('clarivate','https://developer.clarivate.com/signup')]:
   session=await post('/v1/browser/sessions',{'access_profile_ref':profile['id']});sid=session['id'];base=f'/v1/browser/{sid}'
   await post(base+'/input',{'action':'navigate','url':url,'epoch':session['epoch']})
   if name=='elsevier':
    await post(base+'/secret-fill',{'ref':'onboarding/email','selector':'input[name="pf.username"]'})
    try:await post(base+'/input',{'action':'click','selector':'button:has-text("Accept only necessary cookies")','epoch':session['epoch']})
    except httpx.HTTPStatusError:pass
    await post(base+'/input',{'action':'click','selector':'button:has-text("Continue")','epoch':session['epoch']})
   else:
    await post(base+'/input',{'action':'click','selector':'button:has-text("Register")','epoch':session['epoch']})
   await asyncio.sleep(2)
   obs=await post(base+'/observe',{})
   record={'name':name,'session':session,'url':obs['url'].split('?')[0],'text':obs['text'][:10000],'elements':obs['elements']}
   print(json.dumps(record,ensure_ascii=False),flush=True);sessions.append(record)
  Path('.ore/reports/onboarding-sessions.json').write_text(json.dumps(sessions,indent=2))
asyncio.run(main())
