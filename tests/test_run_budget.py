import pytest
from ore.run_budget import RunBudget, BudgetExhausted
from ore.store import Store

@pytest.fixture
def budget(tmp_path):
    store=Store('sqlite:///'+str(tmp_path/'state.sqlite'));store.initialize()
    clock=[100.0]
    value=RunBudget(store,'run',{'max_seconds':100,'max_tokens':1000,'max_turns':3},clock=lambda:clock[0])
    yield value,store,clock
    store.close()

def test_restart_pause_does_not_reset_usage_or_automatic_downtime(budget):
    b,s,clock=budget;b.begin_model('p','planning');b.observe('codex','thread',{'totalTokens':300})
    clock[0]=120;b.pause();clock[0]=200
    again=RunBudget(s,'run',{'max_tokens':999999},clock=lambda:clock[0])
    assert again.snapshot()['remaining_tokens']==700
    assert again.snapshot()['elapsed_seconds']==20
    again.resume();clock[0]=210
    assert again.snapshot()['elapsed_seconds']==30
    again=RunBudget(s,'run',clock=lambda:clock[0]);clock[0]=290
    with pytest.raises(BudgetExhausted,match='time'):again.authorize('repair')

def test_cumulative_deltas_duplicates_and_new_scope_binding(budget):
    b,s,c=budget;b.bind_session('codex','thread',{'totalTokens':100})
    b.observe('codex','thread',{'totalTokens':250});b.observe('codex','thread',{'totalTokens':250})
    b.observe('codex','other',{'totalTokens':80})
    b.bind_session('codex','thread',{'totalTokens':0})
    b.observe('codex','thread',{'totalTokens':300})
    assert b.snapshot()['tokens']['totalTokens']==280

def test_actual_overshoot_blocks_tools_repairs_and_future_models(budget):
    b,s,c=budget;b.begin_model('one');b.observe('codex','t',{'totalTokens':1200})
    assert b.snapshot()['token_overshoot']==200
    for phase in ('tool','repair','calibration','planning'):
        with pytest.raises(BudgetExhausted,match='token'):b.authorize(phase)
    with pytest.raises(BudgetExhausted):b.begin_model('two')
    assert b.snapshot()['provider_turns']==1

def test_known_zero_is_not_missing_usage_and_missing_can_reconcile(budget):
    b,s,c=budget;assert b.snapshot()['usage_complete'] and b.snapshot()['zero_model_calls']
    b.begin_model('one');b.mark_usage_incomplete('codex','t')
    with pytest.raises(BudgetExhausted,match='accounting'):b.authorize()
    b.observe('codex','t',{'inputTokens':10,'outputTokens':2})
    assert b.authorize()['tokens']['totalTokens']==12

def test_counter_regression_never_refunds_usage(budget):
    b,s,c=budget;b.observe('codex','t',{'totalTokens':600});b.observe('codex','t',{'totalTokens':5})
    assert b.snapshot()['tokens']['totalTokens']==600
    with pytest.raises(BudgetExhausted,match='accounting'):b.authorize()

def test_shared_turn_ceiling_and_approved_budget_increase(budget):
    b,s,c=budget
    for i in range(3):b.begin_model(str(i),'repair')
    b.begin_model('2','repair')
    with pytest.raises(BudgetExhausted,match='turn'):b.begin_model('3')
    with pytest.raises(ValueError):b.set_limits({'max_turns':4})
    b.set_limits({'max_turns':4},approved=True);b.begin_model('3')
    assert b.snapshot()['provider_turns']==4


def test_transport_clamped_regression_cannot_be_cleared_by_later_usage(budget):
    b,s,c=budget
    b.observe('codex','t',{'totalTokens':10})
    b.mark_counter_regression('codex','t')
    b.observe('codex','t',{'totalTokens':15})
    assert b.snapshot()['tokens']['totalTokens']==15
    with pytest.raises(BudgetExhausted,match='accounting'):b.authorize()


def test_actual_byte_observations_survive_resume_and_deduplicate_browser_commit(budget):
    b,s,c=budget;b.set_limits({'max_bytes':1000})
    b.observe_bytes(600,observation_id='browser-download-1')
    b.observe_bytes(600,observation_id='browser-download-1')
    b.pause();b.resume()
    b.observe_bytes(500)
    assert b.snapshot()['bytes']==1100 and b.snapshot()['byte_overshoot']==100
    with pytest.raises(BudgetExhausted,match='bytes'):b.authorize()
