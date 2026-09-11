import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright
BASE='http://127.0.0.1:8765'
ROOT=Path('/workspace/ore')
token=(ROOT/'.ore/operator.token').read_text().strip()
client=httpx.Client(base_url=BASE,headers={'Authorization':'Bearer '+token},timeout=30)
profile={'id':'ui-v02-fixture','name':'UI v0.2 isolated fixture','sources':{}}
response=client.post('/v1/access-profiles',json=profile);response.raise_for_status()
response=client.post('/v1/sources/scopus/setup',json={'operation':'search','access_profile_ref':profile['id']});response.raise_for_status();handoff=response.json()
errors=[];checks=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1500,'height':1050})
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(BASE+handoff['href'],wait_until='networkidle')
    page.get_by_label('Operator token').fill(token)
    page.get_by_role('button',name='Connect workspace',exact=True).click()
    page.get_by_role('heading',name='Needs your input',exact=True).wait_for()
    assert page.url.endswith(handoff['href']);checks.append('deep_link_preserved_after_login')
    page.get_by_role('button',name='Open provider browser',exact=True).wait_for()
    page.get_by_text('Use an existing account first',exact=True).wait_for();checks.append('existing_account_first_setup')
    page.get_by_role('button',name='Mark approval pending',exact=True).click()
    page.get_by_text('Waiting External',exact=True).wait_for();checks.append('fixture_provider_pending')
    page.reload(wait_until='networkidle')
    page.get_by_role('heading',name='Needs your input',exact=True).wait_for();checks.append('durable_handoff_reload_cookie_auth')
    page.get_by_role('button',name='Inspect mission',exact=True).click()
    page.get_by_text('Estimated processing time',exact=True).wait_for()
    page.get_by_text('Waiting Provider',exact=True).first.wait_for();checks.append('provider_wait_eta_without_finish_claim')
    page.goto(BASE+'/connections',wait_until='networkidle')
    page.get_by_label('Profile for source readiness').select_option('institution-onboarding')
    page.get_by_text('Approval Pending',exact=True).first.wait_for();checks.append('stored_institution_readiness_visible')
    page.goto(BASE+handoff['href'],wait_until='networkidle')
    page.get_by_role('heading',name='Needs your input',exact=True).wait_for()
    page.screenshot(path=str(ROOT/'.ore/reports/ui-v02.png'),full_page=True)
    page.get_by_role('button',name='Exclude search for this mission',exact=True).click()
    page.get_by_text('Resolved',exact=True).wait_for();checks.append('fixture_pending_search_excluded')
    browser.close()
report={'checks':checks,'browser_errors':errors,'fixture_job_id':handoff['job_id'],'fixture_handoff_id':handoff['id'],'screenshot':'.ore/reports/ui-v02.png','external_provider_requests':0,'existing_profile_mutations':0}
(ROOT/'.ore/reports/ui-v02.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
assert not errors
