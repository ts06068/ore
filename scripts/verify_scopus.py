"""One metadata request using the newly issued key; no metadata is sent to an LLM."""
import asyncio,json
from pathlib import Path
from datetime import datetime,timezone
from ore.config import Settings,SecretStore
from ore_scholarly import search
async def main():
 secret=SecretStore(Settings().prepare().state_dir)
 try:
  result=await search('scopus','DOI(10.4258/hir.2024.30.3.266)',limit=1,config={'query_mode':'native','api_key_ref':'institution/scopus','secret_resolver':secret.get})
  output={'source':'scopus','time':datetime.now(timezone.utc).isoformat(),'status':'success','records':len(result.get('records',[])),'total':result.get('total'),'matches_expected_doi':any(r.get('doi','').lower()=='10.4258/hir.2024.30.3.266' for r in result.get('records',[])),'llm_content_transfer':False}
 except Exception as exc:
  output={'source':'scopus','time':datetime.now(timezone.utc).isoformat(),'status':'failed','error_type':type(exc).__name__,'code':getattr(exc,'code',None),'message':str(exc)[:500],'llm_content_transfer':False}
 Path('.ore/reports/scopus-live-validation.json').write_text(json.dumps(output,indent=2));print(json.dumps(output))
asyncio.run(main())
