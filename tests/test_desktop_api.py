"""Native attach transaction tests: no Docker, browser, network, or model calls."""
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ore.desktop import DesktopError
from ore.desktop_api import create_desktop_router
from ore.policy import AccessDenied, AccessPolicy
from ore.store import DocumentConflict, LeaseLost
from test_engine import engine, mission


class NativeBrowserFixture:
    def __init__(self, engine, job, profile):
        self.engine=engine;self.calls=[];self.fail=None;self.observed_url=None;self.on_observe=None
        self.page_state='content';self.close_old_fails=False
        old=SimpleNamespace(id='old',job_id=job['id'],profile_id=profile['id'],
            principal_id=profile.get('principal_id','operator'),context=SimpleNamespace(desktop=False),
            page=SimpleNamespace(url='https://journal.test/issue',latest={}),closed=False,
            epoch=2,control='human',human_id='operator',mission=deepcopy(job['mission']),
            policy=AccessPolicy(job['mission'],profile))
        self.sessions={'old':old}

    async def create(self,job_id,mission,profile,*,agent_id=None):
        self.calls.append(('create',deepcopy(mission),deepcopy(profile),agent_id))
        if self.fail:raise self.fail
        count=sum(not s.closed and s.profile_id==profile['id'] for s in self.sessions.values())
        if count>=profile.get('max_browser_sessions',1):raise AccessDenied('Access profile browser session limit reached')
        value=SimpleNamespace(id='new',job_id=job_id,profile_id=profile['id'],
            principal_id=profile.get('principal_id','operator'),context=SimpleNamespace(desktop=True),
            page=SimpleNamespace(url='about:blank',latest={}),closed=False,epoch=1,control='agent',
            human_id=None,agent_id=agent_id,mission=deepcopy(mission),policy=AccessPolicy(mission,profile))
        self.sessions[value.id]=value
        return value

    async def takeover(self,sid,user_id='operator'):
        self.calls.append(('takeover',sid));s=self.sessions[sid];s.control='human';s.human_id=user_id;s.epoch+=1
        self.engine.handoffs.create(s.job_id,'browser','Native input',session_id=sid,
            context={'url':s.page.url,'epoch':s.epoch})
        return {'epoch':s.epoch}

    async def action(self,sid,action,args,*,owner):
        self.calls.append(('action',sid,action,owner));s=self.sessions[sid]
        assert s.control=='human' and owner=='human'
        s.page.url=args['url']

    async def observe(self,sid,*,owner,screenshot):
        self.calls.append(('observe',sid));s=self.sessions[sid]
        url=self.observed_url or s.page.url
        s.page.latest={'url':url,'url_observed':True,'screenshot_sha256':'a'*64}
        if self.on_observe:self.on_observe()
        return {'page_state':self.page_state,'url':url}

    async def close_session(self,sid):
        self.calls.append(('close',sid))
        if sid=='old' and self.close_old_fails:raise RuntimeError('fixture cleanup failure')
        self.sessions[sid].closed=True

    async def close(self):pass


@pytest.fixture
async def setup(engine):
    profile={'id':'public','principal_id':'fixture-principal','max_browser_sessions':2,
        'allow_private_network':True,'browser_support_origins':['https://support.test'],
        'retrieval_policy':{'mode':'api_open_access_first','browser_fallback':False}}
    engine.save_profile(profile)
    value=mission(allowed_origins=['https://journal.test'],urls=['https://journal.test/issue'],
        scope={'asset_origins':['https://assets.test'],'browser_support_origins':['https://challenge.test']},
        retrieval_policy={'mode':'api_open_access_first','browser_fallback':False})
    job=engine.create(value,queued=False)
    task=engine.store.create_task(job['id'],'retrieve',{},'held')
    h=engine.handoffs.create(job['id'],'challenge','Existing request',session_id='old',task_id=task['id'],
        context={'url':'https://journal.test/issue','challenge_id':'existing-challenge','epoch':2})
    browser=NativeBrowserFixture(engine,job,profile);engine.browser=browser
    app=FastAPI();app.include_router(create_desktop_router(engine))
    @app.exception_handler(AccessDenied)
    async def denied(request,exc):return JSONResponse({'detail':str(exc)},403)
    @app.exception_handler(LeaseLost)
    @app.exception_handler(DocumentConflict)
    async def conflict(request,exc):return JSONResponse({'detail':str(exc)},409)
    @app.exception_handler(DesktopError)
    async def desktop(request,exc):return JSONResponse({'detail':str(exc)},503)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://ore.test') as client:
        yield engine,client,job,h,browser,profile


def payload(h,key='desktop-attach-001'):
    return {'handoff_id':h['id'],'expected_version':h['state_version'],'idempotency_key':key}


def auth():return {'Authorization':'Bearer synthetic-test-operator'}


async def test_operator_authentication_and_cross_origin_before_browser_creation(setup):
    engine,client,job,h,browser,profile=setup
    assert (await client.post('/v1/desktop/attach',json=payload(h))).status_code==401
    response=await client.post('/v1/desktop/attach',json=payload(h),headers={**auth(),'Origin':'https://attacker.invalid'})
    assert response.status_code==403 and browser.calls==[]
    engine.execution.enabled=True
    assert (await client.post('/v1/desktop/attach',json=payload(h),headers=auth())).status_code==403
    engine.execution.enabled=False
    assert browser.calls==[]


async def test_attach_commits_observed_human_desktop_then_closes_old_and_keeps_contract(setup):
    engine,client,job,h,browser,profile=setup
    before_job=deepcopy(engine.store.get_job(job['id']));before_profile=deepcopy(engine.profile(job['mission']))
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==200,response.text
    value=response.json()
    assert value['session_id']=='new' and value['browser_transport']=='desktop_chrome'
    assert value['status']=='claimed' and value['challenge_id']=='existing-challenge'
    assert browser.sessions['new'].control=='human' and browser.sessions['old'].closed
    assert [c[0] for c in browser.calls]==['create','takeover','action','observe','close']
    create=browser.calls[0]
    assert create[1]['retrieval_policy']['browser_fallback'] is True
    assert create[2]['retrieval_policy']['browser_fallback'] is True
    assert create[2]['require_desktop'] is True and create[2]['browser_backend']=='desktop_chrome'
    assert create[2]['max_browser_sessions']==2 and create[2]['principal_id']=='fixture-principal'
    assert create[3]=='task:'+h['task_id']
    assert value['desktop_attachment']['added_native_origins']==['https://assets.test','https://challenge.test','https://support.test']
    assert value['desktop_attachment']['support_only_enforcement'] is False
    assert engine.store.get_job(job['id'])==before_job and engine.profile(job['mission'])==before_profile
    active=engine.handoffs.list(job['id'],'active')
    assert len(active)==1 and active[0]['id']==h['id']
    replay=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert replay.status_code==200 and len(browser.calls)==5


async def test_fresh_click_reuses_existing_native_session_without_new_container(setup):
    engine,client,job,h,browser,profile=setup
    first=(await client.post('/v1/desktop/attach',json=payload(h),headers=auth())).json()
    response=await client.post('/v1/desktop/attach',json=payload(first,'desktop-attach-002'),headers=auth())
    assert response.status_code==200,response.text
    assert len([c for c in browser.calls if c[0]=='create'])==1
    assert response.json()['desktop_attachment']['reused_session'] is True


@pytest.mark.parametrize('failure',[DesktopError('host cgroup does not support requested resource limits'),AccessDenied('Access profile browser session limit reached')])
async def test_startup_or_capacity_failure_preserves_old_and_no_override(setup,failure):
    engine,client,job,h,browser,profile=setup
    browser.fail=failure
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code in (403,503)
    current=engine.handoffs.get(h['id'])
    assert current['session_id']=='old' and current['challenge_id']=='existing-challenge'
    assert current.get('inflight') is None and not browser.sessions['old'].closed
    assert browser.calls[0][2]['max_browser_sessions']==2


@pytest.mark.parametrize('page_state,observed_url',[('error',None),('content','https://journal.test/different')])
async def test_wrong_checkpoint_or_error_cleans_new_preserves_old(setup,page_state,observed_url):
    engine,client,job,h,browser,profile=setup
    browser.page_state=page_state;browser.observed_url=observed_url
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==403,response.text
    assert browser.sessions['new'].closed and not browser.sessions['old'].closed
    assert engine.handoffs.get(h['id'])['session_id']=='old'
    assert len(engine.handoffs.list(job['id'],'active'))==1


async def test_challenge_page_can_be_attached_without_resolving_or_resetting_budget(setup):
    engine,client,job,h,browser,profile=setup
    browser.page_state='challenge'
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==200 and response.json()['challenge_id']=='existing-challenge'
    assert response.json()['status']=='claimed'
    assert engine.store.get_job(job['id'])['status']=='draft'


async def test_source_exclusion_and_profile_origins_remain_authoritative(setup):
    engine,client,job,h,browser,profile=setup
    narrowed={**profile,'source_policy':{'exclude':{'browser':['general_web']}}}
    engine.save_profile(narrowed)
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==403 and browser.calls==[]
    engine.save_profile({**profile,'origins':['https://journal.test']})
    current=engine.handoffs.get(h['id'])
    response=await client.post('/v1/desktop/attach',json=payload(current,'desktop-attach-002'),headers=auth())
    assert response.status_code==403 and browser.calls==[]


async def test_midflight_revision_change_preserves_original_browser(setup):
    engine,client,job,h,browser,profile=setup
    browser.on_observe=lambda:engine.store.revise_job(job['id'],{**job['mission'],'goal':'Revised fixture'})
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==409,response.text
    assert not browser.sessions['old'].closed and browser.sessions['new'].closed


async def test_cleanup_failure_after_commit_does_not_undo_attachment(setup):
    engine,client,job,h,browser,profile=setup
    browser.close_old_fails=True
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==200 and response.json()['session_id']=='new'
    assert not browser.sessions['new'].closed
    assert any(e['type']=='desktop.old_session_cleanup_failed' for e in engine.store.events(job['id']))


async def test_native_attach_clears_stale_automation_warning(setup):
    engine,client,job,h,browser,profile=setup
    h=engine.handoffs.update(h['id'],{'browser_environment':{'code':'automation_signal_observed','message':'old environment'}})
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==200,response.text
    assert response.json()['browser_environment'] is None


async def test_live_desktop_is_not_reused_after_scope_policy_changed(setup):
    engine,client,job,h,browser,profile=setup
    first=(await client.post('/v1/desktop/attach',json=payload(h),headers=auth())).json()
    changed=deepcopy(engine.profile(job['mission']));changed['browser_support_origins']=[]
    engine.save_profile(changed)
    response=await client.post('/v1/desktop/attach',json=payload(first,'desktop-attach-002'),headers=auth())
    assert response.status_code==403,response.text
    assert not browser.sessions['new'].closed
    assert len([c for c in browser.calls if c[0]=='create'])==1


async def test_close_current_native_desktop_is_idempotent_and_preserves_mission_budget(setup):
    engine,client,job,h,browser,profile=setup
    before=deepcopy(engine.store.get_job(job['id']))
    attached=(await client.post('/v1/desktop/attach',json=payload(h),headers=auth())).json()
    request=payload(attached,'desktop-close-001')
    response=await client.post('/v1/desktop/close',json=request,headers=auth())
    assert response.status_code==200,response.text
    assert response.json()['session_state']=='lost' and response.json()['status']=='needs_user'
    assert response.json()['challenge_id']=='existing-challenge'
    assert browser.sessions['new'].closed and engine.store.get_job(job['id'])==before
    replay=await client.post('/v1/desktop/close',json=request,headers=auth())
    assert replay.status_code==200
    assert len([c for c in browser.calls if c==('close','new')])==1


@pytest.mark.parametrize('mismatch',['foreign_job','other_principal','not_native','wrong_epoch'])
async def test_close_rejects_foreign_non_native_or_changed_control(setup,mismatch):
    engine,client,job,h,browser,profile=setup
    attached=(await client.post('/v1/desktop/attach',json=payload(h),headers=auth())).json()
    session=browser.sessions['new']
    if mismatch=='foreign_job':session.job_id='other-job'
    elif mismatch=='other_principal':session.principal_id='other-principal'
    elif mismatch=='not_native':session.context.desktop=False
    else:session.epoch+=1
    response=await client.post('/v1/desktop/close',json=payload(attached,'desktop-close-001'),headers=auth())
    assert response.status_code in (403,409),response.text
    assert not session.closed


async def test_close_requires_operator_authentication(setup):
    engine,client,job,h,browser,profile=setup
    response=await client.post('/v1/desktop/close',json=payload(h))
    assert response.status_code==401 and not browser.calls

def test_only_historical_support_probe_is_omitted_and_originals_stay_unchanged():
    from ore.desktop_api import _session_policy
    probe = 'https://brunhild.challenges.cloudflare.com'
    mission = {'allowed_origins': ['https://journal.test'], 'scope': {'browser_support_origins': [probe, 'https://challenges.cloudflare.com', 'https://other.challenges.cloudflare.com']}}
    original = deepcopy(mission)
    copy, profile, additions = _session_policy(mission, {})
    assert mission == original
    assert probe not in copy['allowed_origins']
    assert 'https://challenges.cloudflare.com' in additions
    assert 'https://other.challenges.cloudflare.com' in additions
    assert copy['desktop_omitted_origins'] == [{'origin': probe, 'reason': 'expected_negative_dns_probe'}]
    mission['allowed_origins'].append(probe)
    copy, _, _ = _session_policy(mission, {})
    assert probe in copy['allowed_origins'] and copy['desktop_omitted_origins'] == []


async def test_preview_validates_scope_without_browser_or_state_mutation(setup):
    engine,client,job,h,browser,profile=setup
    before=deepcopy(engine.handoffs.get(h['id']))
    result=await client.get('/v1/desktop/preview',params={'handoff_id':h['id']},headers=auth())
    assert result.status_code==200,result.text
    preview=result.json()
    assert preview['eligible'] and preview['basis']=='mission'
    assert set(preview['origins'])=={'https://journal.test','https://assets.test','https://challenge.test','https://support.test'}
    assert preview['expected_version']==h['state_version'] and browser.calls==[]
    assert engine.handoffs.get(h['id'])==before
    engine.save_profile({**profile,'origins':['https://journal.test']})
    denied=await client.get('/v1/desktop/preview',params={'handoff_id':h['id']},headers=auth())
    assert denied.status_code==403 and browser.calls==[]


async def test_legacy_taskless_operator_request_uses_saved_checkpoint_only(setup):
    engine,client,_,_,_,profile=setup
    job=engine.create({'goal':'Legacy manual access','sources':[],'artifact_roles':[], 'allowed_origins':[]},queued=False)
    h=engine.handoffs.create(job['id'],'challenge','Existing manual request',session_id='old',
        context={'url':'https://journal.test/issue','challenge_id':'preserved-counter','epoch':2})
    browser=NativeBrowserFixture(engine,job,profile);engine.browser=browser
    before_job=deepcopy(engine.store.get_job(job['id']))
    before_challenges=deepcopy(engine.store.list_documents('challenge',job_id=job['id']))
    preview=await client.get('/v1/desktop/preview',params={'handoff_id':h['id']},headers=auth())
    assert preview.status_code==200,preview.text
    assert preview.json()['basis']=='persisted_manual_checkpoint'
    response=await client.post('/v1/desktop/attach',json=payload(h),headers=auth())
    assert response.status_code==200,response.text
    assert response.json()['challenge_id']=='preserved-counter'
    assert response.json()['desktop_attachment']['scope_basis']=='persisted_manual_checkpoint'
    assert browser.calls[0][1]['allowed_origins']==['https://journal.test','https://support.test']
    assert engine.store.get_job(job['id'])==before_job
    assert engine.store.list_documents('challenge',job_id=job['id'])==before_challenges
