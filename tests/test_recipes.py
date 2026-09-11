import asyncio
import pytest
from ore.recipes import execute_recipe, evaluate, RecipeCatalog, RecipeError
from ore.store import Store
from ore.models import canonical_digest


def program():
    return {'version':1,'steps':[{'id':'items','items':{'$ref':'inputs.items'},'steps':[
        {'id':'get','tool':'fetch','inputs':{'n':{'$ref':'item'}}},
        {'id':'save','tool':'save','inputs':{'value':{'$ref':'steps.get.value'}},
         'checks':[{'$eq':[{'$ref':'output.saved'},True]}]}]}],
        'output':{'$len':{'$ref':'steps.items'}}}


async def test_bounded_parallel_program_and_crash_resume_preserve_completed_effects():
    receipts={};writes=[];active=0;peak=0;checkpoints=[]
    async def call(tool,args,op):
        nonlocal active,peak
        if op in receipts:return receipts[op]
        active+=1;peak=max(active,peak)
        try:
            await asyncio.sleep(.001)
            if tool=='fetch':result={'value':args['n']}
            else:writes.append(args['value']);result={'saved':True}
            receipts[op]=result;return result
        finally:active-=1
    async def checkpoint(value):
        checkpoints.append(value)
        if len(checkpoints)==7:raise RuntimeError('process stopped')
    with pytest.raises(RuntimeError):
        await execute_recipe(program(),{'items':list(range(30))},call,on_checkpoint=checkpoint,max_concurrency=5)
    finished=await execute_recipe(program(),{'items':list(range(30))},call,checkpoint=checkpoints[-1],max_concurrency=5)
    assert finished['output']==30 and finished['model_calls']==0
    assert sorted(writes)==list(range(30)) and peak<=5
    with pytest.raises(RecipeError,match='different'):
        await execute_recipe(program(),{'items':[1]},call,checkpoint=finished['checkpoint'])


async def test_step_failures_and_exhaustion_do_not_claim_completion():
    async def call(*args):return {'saved':False}
    p={'version':1,'steps':[{'id':'write','tool':'save','checks':[{'$eq':[{'$ref':'output.saved'},True]}]}]}
    with pytest.raises(RecipeError,match='check failed'):await execute_recipe(p,{},call)
    async def call(*args):return {'value':1,'saved':True}
    with pytest.raises(RecipeError,match='budget'):await execute_recipe(program(),{'items':[1,2]},call,max_steps=2)


def test_expressions_are_data_only_and_formats_cannot_traverse_objects():
    assert evaluate({'$format':{'template':'item-{n:02d}.txt','values':{'n':3}}},{})=='item-03.txt'
    assert evaluate({'$urljoin':['https://example.org/a/','b']},{})=='https://example.org/a/b'
    for template in ('{n.__class__}', '{n[0]}', '{n:999999999d}', '{n!r}'):
        with pytest.raises(RecipeError):evaluate({'$format':{'template':template,'values':{'n':3}}},{})
    with pytest.raises(RecipeError):evaluate({'$eval':'open("/etc/passwd")'},{})


async def test_only_five_distinct_independently_verified_cases_allow_matching_contract_reuse(tmp_path):
    store=Store('sqlite:///'+str(tmp_path/'state.sqlite'));store.initialize()
    catalog=RecipeCatalog(store)
    contract=catalog.contract(tools={'fetch':'v1'},source='source-v1',validator='exact-v1',access='public-v1')
    row=catalog.propose(program(),contract=contract,owner='owner')
    def verify(inputs,evidence):return {'passed':evidence==inputs['n']*2,'validator_digest':'exact-v1','case_digest':canonical_digest({'n':inputs['n']})}
    for _ in range(5):await catalog.verify(row['id'],inputs={'n':0},evidence=0,verifier=verify)
    assert not catalog.can_replay(row['id'],contract=contract,owner='owner')
    for n in range(1,5):await catalog.verify(row['id'],inputs={'n':n},evidence=n*2,verifier=verify)
    assert catalog.can_replay(row['id'],contract=contract,owner='owner')
    assert not catalog.can_replay(row['id'],contract='changed',owner='owner')
    assert not catalog.can_replay(row['id'],contract=contract,owner='different-user')
    catalog.reused(row['id'],contract=contract,owner='owner')
    await catalog.verify(row['id'],inputs={'n':5},evidence=-1,verifier=verify)
    assert not catalog.can_replay(row['id'],contract=contract,owner='owner')
    store.close()
