import json
from types import SimpleNamespace
import httpx
from ore.mcp import MCPAdapter
from ore.progress import progress_snapshot


def fixture_store(count=5):
    rows=[{'id':str(i),'kind':'article','state':'succeeded' if i<count else 'pending','revision':1,'generation':1} for i in range(count+2)]
    return SimpleNamespace(tasks=lambda job:rows,artifacts=lambda job:[],resources=lambda job:[],
        attempts=lambda ident:[{'state':'succeeded','started_at':'2026-01-01T00:00:00Z','finished_at':'2026-01-01T00:00:10Z'}] if int(ident)<count else [])


def test_eta_requires_verified_inventory_and_real_samples():
    job={'id':'job','revision':1,'generation':1,'status':'running'}
    store=fixture_store()
    assert progress_snapshot(store,job,audit={'complete':True})['eta']['state']=='estimating'
    assert progress_snapshot(fixture_store(4),job,audit={'inventory_verified':True,'coverage_denominator':2})['eta']['state']=='estimating'
    result=progress_snapshot(store,job,audit={'inventory_verified':True,'coverage_denominator':2})
    assert result['eta']['remaining_active_seconds']=={'low':20,'high':20}
    waiting=progress_snapshot(store,job,[{'status':'waiting_external'}],audit={'inventory_verified':True,'coverage_denominator':2})
    assert waiting['eta']['state']=='waiting_provider' and waiting['eta']['finish_at'] is None


async def test_mcp_initialize_status_links_and_no_credential_tools():
    requests=[]
    def transport(request):
        requests.append(request)
        return httpx.Response(200,json={'id':'job','status':'awaiting_user','href':'/jobs/job','handoffs':[{'id':'h','href':'/handoffs/h'}],'progress':{'eta':{'state':'waiting_user'}}})
    async with httpx.AsyncClient(base_url='https://ore.test',transport=httpx.MockTransport(transport)) as client:
        adapter=MCPAdapter('https://ore.test','synthetic-only',client=client)
        init=await adapter.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})
        assert init['result']['capabilities']=={'tools':{}}
        listing=await adapter.handle({'id':2,'method':'tools/list'})
        assert all('secret' not in t['name'] for t in listing['result']['tools'])
        response=await adapter.handle({'id':3,'method':'tools/call','params':{'name':'ore_wait','arguments':{'job_id':'job','seconds':20}}})
        value=json.loads(response['result']['content'][0]['text'])
        assert value['needs_user'] and value['handoffs'][0]['href']=='https://ore.test/handoffs/h'
        assert len(requests)==1
        assert await adapter.handle({'method':'notifications/initialized'}) is None


async def test_mcp_does_not_echo_provider_error_body_or_credentials():
    async with httpx.AsyncClient(base_url='https://ore.test',transport=httpx.MockTransport(lambda req:httpx.Response(403,text='private-provider-key'))) as client:
        adapter=MCPAdapter('https://ore.test','synthetic',client=client)
        response=await adapter.handle({'id':1,'method':'tools/call','params':{'name':'ore_status','arguments':{'job_id':'j'}}})
        assert response['result']['isError']
        assert 'private-provider-key' not in json.dumps(response)


def test_nonrunning_states_never_forecast_even_with_inventory_and_enough_samples():
    expected={'paused_budget':('paused_budget','paused_budget'),
        'awaiting_source':('waiting_source','waiting_source'),
        'finished_incomplete':('incomplete','finished_incomplete'),
        'needs_review':('needs_review','needs_review'),
        'failed':('failed','failed'),'waiting_external':('waiting_provider','waiting_provider'),
        'queued':('queued','queued')}
    for status,(eta_state,stage) in expected.items():
        store=fixture_store();rows=store.tasks('job');rows[-1]['state']='running'
        result=progress_snapshot(store,{'id':'job','revision':1,'generation':1,'status':status},
            audit={'inventory_verified':True,'coverage_denominator':2})
        assert result['eta']['state']==eta_state,status
        assert result['stage']==stage,status
        assert result['eta']['remaining_active_seconds'] is None,status
        assert result['eta']['finish_at'] is None,status


def test_waiting_task_blocks_forecast_before_job_state_catches_up():
    for waiting,state in [('paused_budget','paused_budget'),('awaiting_source','waiting_source')]:
        store=fixture_store();store.tasks('job')[-1]['state']=waiting
        result=progress_snapshot(store,{'id':'job','revision':1,'generation':1,'status':'running'},
            audit={'inventory_verified':True,'coverage_denominator':2})
        assert result['eta']['state']==state
        assert result['eta']['remaining_active_seconds'] is None


def test_old_handoff_does_not_block_current_revision_eta():
    result=progress_snapshot(fixture_store(),{'id':'job','revision':1,'generation':1,'status':'running'},
        [{'status':'needs_user','revision':0,'generation':1}],
        audit={'inventory_verified':True,'coverage_denominator':2})
    assert not result['needs_user'] and result['eta']['state']=='available'
