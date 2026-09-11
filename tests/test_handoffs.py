"""Durable intervention, source onboarding and no-secret HTTP contracts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_engine import engine, mission
from test_server import client, login
from ore.handoffs import HandoffService, safe_checkpoint
from ore.store import DocumentConflict, LeaseLost


def test_safe_checkpoint_drops_ephemeral_credentials():
    assert safe_checkpoint('https://journal.test/paper?token=private#frag')=='https://journal.test/paper'
    assert safe_checkpoint('https://name:password@journal.test/paper') is None
    assert safe_checkpoint('file:///private') is None


async def test_durable_handoff_cas_idempotency_and_restart(engine):
    job=engine.create(mission(),queued=False)
    service=HandoffService(engine.store)
    row=service.create(job['id'],'challenge','Please verify',session_id='missing',context={'url':'https://journal.test/p?key=secret','epoch':3})
    replay=HandoffService(engine.store).get(row['id'])
    assert replay['href']=='/handoffs/'+row['id']
    assert 'secret' not in str(replay)
    first,repeated=service.begin_action(row['id'],'cancel',row['state_version'],'request-001')
    assert not repeated
    with pytest.raises((DocumentConflict,LeaseLost)):service.begin_action(row['id'],'claim',row['state_version'],'request-002')
    done=service.finish_action(row['id'],'cancel','request-001',{'status':'cancelled'})
    again,repeated=service.begin_action(row['id'],'cancel',row['state_version'],'request-001')
    assert repeated and again['state_version']==done['state_version']
    assert 'receipts' not in service.public(done)


async def test_lost_session_deeplink_and_authenticated_actions(client,engine):
    job=engine.create(mission(),queued=False)
    row=engine.handoffs.create(job['id'],'browser','Sign in',session_id='old-session',context={'url':'https://journal.test/'})
    assert (await client.get('/v1/handoffs')).status_code==401
    # UI shell deep links are public; all data remains behind the token login.
    assert (await client.get(row['href'])).status_code==200
    await login(client)
    request=(await client.get('/v1/handoffs/'+row['id'])).json()
    assert request['session_state']=='lost'
    detail=(await client.get('/v1/jobs/'+job['id'])).json()
    assert detail['progress']['needs_user']
    assert detail['progress']['eta']['state']=='draft'
    bad=await client.post('/v1/handoffs/'+row['id']+'/actions',json={'action':'cancel','expected_version':0,'idempotency_key':'test-request'})
    assert bad.status_code==422
    action={'action':'cancel','expected_version':request['state_version'],'idempotency_key':'test-request'}
    result=await client.post('/v1/handoffs/'+row['id']+'/actions',json=action)
    assert result.status_code==200,result.text
    assert (await client.post('/v1/handoffs/'+row['id']+'/actions',json=action)).json()['status']=='cancelled'


async def test_selected_provider_setup_pending_and_source_exclusion(client,engine):
    await login(client)
    response=await client.post('/v1/sources/wos/setup',json={'operation':'search','access_profile_ref':'public'})
    assert response.status_code==200,response.text
    row=response.json()
    assert row['source']=='wos' and row['session_id'] is None
    assert row['access_profile_ref']=='connection-wos'
    assert 'existing provider account' in row['reason']
    assert not hasattr(engine.browser, 'create')
    result=await client.post('/v1/handoffs/'+row['id']+'/actions',json={'action':'mark_pending','expected_version':row['state_version'],'idempotency_key':'pending-request'})
    assert result.status_code==200,result.text
    row=result.json()
    sources=(await client.get('/v1/sources?access_profile_id=connection-wos')).json()
    wos=next(s for s in sources if s['id']=='wos')
    assert wos['operation_readiness']['search']['state']=='approval_pending'
    assert not wos['live_verified']
    response=await client.post('/v1/handoffs/'+row['id']+'/actions',json={'action':'exclude_operation','expected_version':row['state_version'],'idempotency_key':'exclude-request'})
    assert response.status_code==200,response.text
    job=engine.store.get_job(row['job_id'])
    assert job['mission']['source_policy']['exclude']['search']==['wos']
    assert 'wos' not in job['mission']['source_policy']['exclude'].get('browser',[])


async def test_issued_key_does_not_prove_source_ready(client,engine):
    await login(client)
    engine.secrets.set('fixture/scopus','synthetic-provider-key')
    engine.save_profile({'id':'institution','sources':{'scopus':{'api_key_ref':'fixture/scopus'}}})
    response=await client.get('/v1/sources?access_profile_id=institution')
    assert 'synthetic-provider-key' not in response.text
    scopus=next(s for s in response.json() if s['id']=='scopus')
    assert scopus['operation_readiness']['search']['state']=='unknown'


async def test_remote_browser_session_is_linked_to_handoff(client,engine):
    await login(client)
    job=engine.create(mission(),queued=False)
    session=SimpleNamespace(id='remote-browser',job_id=job['id'],epoch=7,control='agent',closed=False)
    summary={'id':session.id,'job_id':job['id'],'epoch':7,'control':'agent','url':'https://journal.test/','worker_id':'executor-fixture'}
    original_execution=engine.execution
    engine.execution=SimpleNamespace(enabled=True,authorize_request=lambda request:False,has_session=lambda sid:sid==session.id,
        get_session=lambda sid:session,session_summary=AsyncMock(return_value=summary),browser_command=AsyncMock(return_value={**summary,'epoch':8,'control':'human'}))
    row=engine.handoffs.create(job['id'],'browser','Use remote browser',session_id=session.id)
    output=(await client.get('/v1/handoffs/'+row['id'])).json()
    assert output['executor_id']=='executor-fixture' and output['control_epoch']==7
    response=await client.post('/v1/handoffs/'+row['id']+'/actions',json={'action':'claim','expected_version':output['state_version'],'expected_epoch':6,'idempotency_key':'claim-stale'})
    assert response.status_code==409
    assert engine.execution.browser_command.await_count==0
    response=await client.post('/v1/handoffs/'+row['id']+'/actions',json={'action':'claim','expected_version':output['state_version'],'expected_epoch':7,'idempotency_key':'claim-current'})
    assert response.status_code==200,response.text
    engine.execution.browser_command.assert_awaited_once_with(session.id,'takeover',{'epoch':7})
    engine.execution=original_execution

async def test_interrupted_action_is_recoverable_and_new_episode_gets_new_id(engine):
    job=engine.create(mission(),queued=False)
    service=engine.handoffs
    row=service.create(job['id'],'browser','First request',session_id='session')
    service.begin_action(row['id'],'claim',row['state_version'],'interrupted-key')
    service.recover_interrupted_actions()
    recovered=service.get(row['id'])
    assert recovered['inflight'] is None and recovered['status']=='needs_user'
    service.update(row['id'],{'status':'resolved'})
    new=service.create(job['id'],'browser','Second request',session_id='session')
    assert new['id']!=row['id'] and new['status']=='needs_user'

async def test_remote_source_check_stays_in_executor_and_stores_only_evidence(client,engine):
    await login(client)
    original=engine.execution
    engine.execution=SimpleNamespace(enabled=True,authorize_request=lambda request:False,
        source_check=AsyncMock(return_value={'items':[{'title':'Do not store provider response'}]}))
    response=await client.post('/v1/sources/pubmed/check',json={'operation':'search','access_profile_ref':'public'})
    assert response.status_code==200,response.text
    assert response.json()['state']=='ready'
    assert response.json()['scope']=='executor_search_bounded_check'
    assert 'Do not store provider response' not in response.text
    engine.execution.source_check.assert_awaited_once()
    engine.execution=original

async def test_prior_revision_handoff_is_historical_and_cannot_control_current_job(client,engine):
    await login(client)
    job=engine.create(mission(),queued=False)
    old=engine.handoffs.create(job['id'],'browser','Old browser request',session_id='expired')
    engine.store.revise_job(job['id'],{**job['mission'],'goal':'New mission revision'})
    assert engine.handoffs.list(job['id'],'active')==[]
    assert engine.handoffs.list(status='active')==[]
    response=await client.get('/v1/handoffs/'+old['id'])
    assert response.status_code==200 and response.json()['status']=='cancelled'
    assert response.json()['superseded']
    persisted=engine.store.get_document('handoff',old['id'])
    assert persisted['revision']==1 and persisted['state_version']==old['state_version']
    action=await client.post('/v1/handoffs/'+old['id']+'/actions',json={
        'action':'recreate_session','expected_version':old['state_version'],'idempotency_key':'old-revision-action'})
    assert action.status_code==409
    current=engine.handoffs.create(job['id'],'browser','Current revision request',session_id='current')
    assert [x['id'] for x in engine.handoffs.list(job['id'],'active')]==[current['id']]
    engine.store.refresh_job(job['id'])
    assert engine.handoffs.list(job['id'],'active')==[]

async def test_source_exclusion_keeps_old_handoffs_out_of_resume_blocklist(client,engine,monkeypatch):
    await login(client)
    job=engine.create(mission(sources=['wos']),queued=False)
    old=engine.handoffs.create(job['id'],'source_setup','Pending API',source='wos',operation='search',task_id='old-held-task')
    second=engine.handoffs.create(job['id'],'browser','Old browser wait',session_id='old-browser',task_id='old-browser-task')
    original=engine.store.reset_for_resume
    blocked=[]
    def reset(ident,**options):
        blocked.extend(options.get('blocked_task_ids',[]))
        return original(ident,**options)
    monkeypatch.setattr(engine.store,'reset_for_resume',reset)
    response=await client.post('/v1/handoffs/'+old['id']+'/actions',json={
        'action':'exclude_operation','expected_version':old['state_version'],'idempotency_key':'exclude-and-revise'})
    assert response.status_code==200,response.text
    assert engine.store.get_job(job['id'])['revision']==2
    assert blocked==[]
    assert engine.handoffs.list(job['id'],'active')==[]
    assert engine.handoffs.get(old['id'])['request_revision']==1
    assert engine.handoffs.public(engine.handoffs.get(second['id']))['superseded']
