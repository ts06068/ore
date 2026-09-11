import copy
import json
import pytest
from ore.conversation import ConversationManager
from ore.planner_context import PlannerContext
from ore.run_budget import RunBudget, BudgetExhausted
from ore.store import Store
from test_conversation import setup, decision, plan, turn


async def test_planning_usage_carries_into_approved_execution_and_repair(setup):
    engine,manager=setup
    ident=manager.create({'budget':{'max_turns':6,'max_tokens':10}})['id']
    engine.backend.responses=[decision('ask',questions=[{'text':'Which source?'}]),decision('propose_plan',**plan())]
    first=await turn(manager,ident,'Collect requested content')
    assert first['usage']['tokens']['totalTokens']==1
    assert first['usage']['paused']
    second=await turn(manager,ident,'Use example.org')
    assert second['usage']['tokens']['totalTokens']==2
    scope=second['plans'][-1]['mission']['budget_scope_id']
    await manager.approve(ident,second['active_plan_id'])
    budget=RunBudget(engine.store,scope)
    assert budget.snapshot()['tokens']['totalTokens']==2
    budget.observe('execution','other',{'totalTokens':9})
    engine.workflows.runs['run-1'].update(status='needs_replan',node_statuses={'extract':'needs_replan'})
    run={**engine.workflows.get_run('run-1'),'nodes':engine.workflows.nodes('run-1')}
    calls=len(engine.backend.prompts)
    await manager._begin_repair(ident,run)
    assert len(engine.backend.prompts)==calls
    assert not manager.get(ident)['planner_running']
    with pytest.raises(BudgetExhausted,match='token'):budget.authorize('repair')


async def test_shared_budget_scope_cannot_be_selected_by_model(setup):
    engine,manager=setup;ident=manager.create()['id']
    engine.backend.responses=[decision('propose_plan',**plan(mission={'budget_scope_id':'some-other-request'}))]
    value=await turn(manager,ident,'Collect requested content')
    assert value['plans'][-1]['mission']['budget_scope_id'].startswith('conversation:'+ident+':')


async def test_same_failure_fingerprint_is_not_repaired_twice(setup):
    engine,manager=setup;ident=manager.create()['id']
    engine.backend.responses=[decision('propose_plan',**plan())]
    initial=await turn(manager,ident,'Prepare extraction')
    await manager.approve(ident,initial['active_plan_id'])
    engine.workflows.runs['run-1'].update(status='needs_replan',node_statuses={'extract':'needs_replan'})
    run={**engine.workflows.get_run('run-1'),'nodes':engine.workflows.nodes('run-1')}
    engine.backend.responses=[decision('propose_plan',**plan())]
    await manager._begin_repair(ident,run);await manager._tasks[ident]
    assert len(engine.workflows.revised)==1
    await manager._begin_repair(ident,run)
    assert len(engine.workflows.revised)==1 and not manager.get(ident)['planner_running']


def test_large_unicode_context_is_bounded_retrievable_and_scoped():
    store=Store('sqlite:///:memory:');store.initialize()
    context=PlannerContext(store,'a')
    original={'user_request':'한글😀'*50000,'scope':{'url':'https://example.org'}}
    packed=context.pack(original)
    assert len(json.dumps(packed,ensure_ascii=False).encode())<=65536
    handle=packed['user_request']['context_ref'];offset=0;chunks=[]
    while True:
        page=context.read(handle,offset);chunks.append(page['text']);offset=page['next_offset']
        if not page['has_more']:break
    assert json.loads(''.join(chunks))==original['user_request']
    with pytest.raises(KeyError):PlannerContext(store,'b').read(handle)
    store.close()
