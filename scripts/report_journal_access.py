import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime,timezone
from ore.config import Settings
from ore.store import Store
store=Store(Settings().prepare().database_url);store.initialize();results=[]
for target in json.loads(Path('.ore/reports/journal-agent-jobs.json').read_text()):
 job=store.get_job(target['job_id']);events=store.events(job['id']);observations=store.observations(job['id'])
 episodes={e['payload'].get('challenge_id') or e['payload'].get('id') for e in events if e['type'].startswith('challenge.')}
 episodes.discard(None)
 counts=[]
 for ident in episodes:
  record=store.get_challenge(ident)
  if record:counts.append({k:v for k,v in record.items() if k in ('origin','attempts','active_seconds','state','max_attempts','max_active_seconds')})
 results.append({**target,'job_status':job['status'],'challenge_episodes':counts,'observed_pages':[{'url':urlunsplit((*urlsplit(x.get('url') or '')[:3], '', '')),'title':x.get('title'),'kind':x.get('kind')} for x in observations if x.get('kind') in ('browser.observation','browser.page')],
  'model_routes':[e['payload'] for e in events if e['type']=='model_selected'],'handoffs':[e['payload'] for e in events if e['type']=='browser_handoff'],'artifact_count':len(store.artifacts(job['id'])),'institutional_fulltext_verified':False})
report={'time':datetime.now(timezone.utc).isoformat(),'mode':'four_real_Codex_host_workers_to_browser_coordinator','results':results,'backfile_entitlement_verified':False}
Path('.ore/reports/journal-live-validation.json').write_text(json.dumps(report,indent=2));print(json.dumps({'journals':len(results),'statuses':[x['job_status'] for x in results],'human_handoffs':[len(x['handoffs']) for x in results],'artifacts':[x['artifact_count'] for x in results]}));store.close()
