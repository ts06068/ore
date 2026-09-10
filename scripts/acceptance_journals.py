"""Create four small institutional-access browser tasks, never a bulk download."""
import asyncio,json
from pathlib import Path
import httpx
async def main():
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer '+Path('.ore/operator.token').read_text().strip()},timeout=60) as c:
  rows=[]
  for name,url in [('JACC','https://www.jacc.org/loi/jacc'),('EHJ','https://academic.oup.com/eurheartj/issue-archive'),('Circulation','https://www.ahajournals.org/loi/circ'),('JAMA Cardiology','https://jamanetwork.com/journals/jamacardiology/currentissue')]:
   mission={'goal':f'Validate authorized institutional access to {name}. Open {url} and inspect actual page. If a bot challenge appears, use the challenge tool and at most three ordinary waiting or visible UI interactions within its budget, then handoff the SAME browser for user intervention if unresolved. Do not use alternate proxy identities or external solvers. If archive access succeeds, register one original research article and try its main PDF and supplementary links. This is one small access validation, not bulk retrieval. Never claim subscription entitlement from HTTP200 alone.',
    'name':name+' access acceptance','urls':[url],'access_profile_ref':'institution-onboarding','artifact_roles':['main_pdf','supplement'],'budget':{'max_turns':12,'max_seconds':360,'max_agent_workers':1},'limits':{'origin_min_interval_seconds':1},'on_challenge':{'max_attempts_per_episode':3,'max_active_seconds':120},'routing':{'mode':'fixed','model':'gpt-6-astra','effort':'high'}}
   r=await c.post('/v1/jobs',json=mission);r.raise_for_status();job=r.json();r=await c.post(f"/v1/jobs/{job['id']}/run",json={});r.raise_for_status();rows.append({'name':name,'job_id':job['id']})
  Path('.ore/reports/journal-agent-jobs.json').write_text(json.dumps(rows,indent=2));print(json.dumps(rows))
asyncio.run(main())
