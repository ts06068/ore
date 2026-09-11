"""Real local browser recovery retains task ownership across a new lease."""
import pytest
from ore.browser import BrowserManager
from ore.handoffs import BrowserAccess
from ore.policy import AccessDenied, RateLimiter
from ore.tools import ToolRuntime
from test_browser import site, browser_dependencies
from test_engine import engine, mission
from test_server import client, login


@pytest.mark.browser
async def test_recreated_browser_human_solve_resumes_original_task_only(client, engine, site, browser_dependencies):
    await login(client)
    engine.save_profile({'id':'public','allow_private_network':True,'sources':{}})
    engine.browser=BrowserManager(engine.settings,RateLimiter(),store=engine.store,secrets=engine.secrets,on_event=engine.event)
    job=engine.create(mission(urls=[site+'/challenge'],allowed_origins=[site],
        on_challenge={'max_attempts_per_episode':1,'max_active_seconds':60},limits={'origin_min_interval_seconds':0}))
    task=engine.store.claim_task('original-worker',job_id=job['id'])
    assert task
    old=await engine.browser.create(job['id'],job['mission'],engine.profile(job['mission']),agent_id='task:'+task['id'])
    await engine.browser.action(old.id,'navigate',{'url':site+'/challenge','screenshot':False})
    assert (await engine.browser.challenge(old.id))['allowed']
    await engine.browser.action(old.id,'click',{'selector':'#retry','epoch':old.epoch,'screenshot':False})
    assert old.control=='human'
    request=engine.handoffs.list(job['id'],'active')[0]
    assert request['task_id']==task['id'] and request['challenge_id']
    original_challenge=request['challenge_id']
    engine.store.fail_task(task['id'],task['worker_id'],task['fence'],{'code':'needs_user'},task['revision'],state='awaiting_user')
    await engine.pause(job['id'],'awaiting_user')
    await engine.browser.close_session(old.id)
    request=(await client.get('/v1/handoffs/'+request['id'])).json()
    response=await client.post('/v1/handoffs/'+request['id']+'/actions',json={
        'action':'recreate_session','expected_version':request['state_version'],'idempotency_key':'recreate-original-task'})
    assert response.status_code==200,response.text
    request=response.json();sid=request['session_id']
    restored=engine.browser.get(sid)
    assert sid!=old.id and restored.agent_id=='task:'+task['id'] and restored.control=='human'
    await engine.browser.action(sid,'click',{'selector':'#solve','epoch':restored.epoch,'screenshot':False},owner='human')
    request=(await client.get('/v1/handoffs/'+request['id'])).json()
    response=await client.post('/v1/handoffs/'+request['id']+'/actions',json={
        'action':'resume','expected_version':request['state_version'],'expected_epoch':request['control_epoch'],'idempotency_key':'resume-original-task'})
    assert response.status_code==200,response.text
    assert response.json()['status']=='resolved'
    assert engine.store.get_challenge(original_challenge)['state']=='resolved'
    resumed=engine.store.claim_task('replacement-worker',job_id=job['id'])
    assert resumed['id']==task['id'] and resumed['fence']>task['fence']
    runtime=ToolRuntime(engine,job['id'],resumed)
    state=await runtime.execute('state',{})
    assert [browser['id'] for browser in state['browsers']]==[sid]
    assert old.id not in str(state['browsers'])
    assert any(item['session_id']==sid for item in state['handoffs'])
    result=await runtime.execute('browser_observe',{'session_id':sid})
    assert not result['challenge_detected'] and result['control']=='agent'
    assert 'independently observed target content' in result['text']
    engine.store.create_task(job['id'],'article',{},'another-task')
    other=engine.store.claim_task('unrelated-worker',job_id=job['id'])
    assert other and other['id']!=task['id']
    other_state=await ToolRuntime(engine,job['id'],other).execute('state',{})
    assert other_state['browsers']==[] and other_state['handoffs']==[]
    with pytest.raises(AccessDenied,match='another agent task'):
        await ToolRuntime(engine,job['id'],other).execute('browser_observe',{'session_id':sid})


async def test_recreation_rejects_task_from_other_job_or_revision(engine):
    from ore.store import LeaseLost
    first=engine.create(mission(),queued=False)
    task=engine.store.tasks(first['id'])[0]
    second=engine.create(mission(),queued=False)
    broker=BrowserAccess(engine)
    with pytest.raises(LeaseLost,match='no longer belongs'):
        await broker.create(second,engine.profile(second['mission']),task_id=task['id'])
    updated=engine.store.revise_job(first['id'],first['mission'])
    with pytest.raises(LeaseLost,match='no longer belongs'):
        await broker.create(updated,engine.profile(updated['mission']),task_id=task['id'])

async def test_remote_recreation_receives_original_task_identity(engine):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    original=engine.execution
    job=engine.create(mission(),queued=False)
    task=engine.store.tasks(job['id'])[0]
    remote=SimpleNamespace(enabled=True,create_session=AsyncMock(return_value={'id':'remote-restored'}))
    engine.execution=remote
    try:
        sid=await BrowserAccess(engine).create(job,engine.profile(job['mission']),task_id=task['id'])
        assert sid=='remote-restored'
        remote.create_session.assert_awaited_once_with(job['id'],job['mission'],engine.profile(job['mission']),
            agent_id='task:'+task['id'],task_id=task['id'])
    finally:
        engine.execution=original
