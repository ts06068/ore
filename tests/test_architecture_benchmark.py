"""Fairness gates must apply before primitive side effects, independently of grading."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
_original_path = list(sys.path)
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.benchmark_architecture import BUDGET, CommonPrimitives, Usage
    from scripts.benchmark_fixtures import BenchmarkFixture
finally:
    sys.path[:] = _original_path


async def test_common_native_batch_capacity_is_five():
    fixture=SimpleNamespace(calls=[],saves=[],restart_after_saves=None)
    async def operation(name,args):
        fixture.calls.append(name);await asyncio.sleep(.01);return {}
    fixture.call=operation
    shared=CommonPrimitives(fixture,Usage(),asyncio.Event(),asyncio.Event())
    await asyncio.gather(*(shared('bench.select',{}) for _ in range(17)))
    assert shared.peak==5 and len(fixture.calls)==17


async def test_cumulative_save_byte_ceiling_prevents_write(monkeypatch):
    monkeypatch.setitem(BUDGET,'max_bytes',5)
    fixture=SimpleNamespace(calls=[],saves=[],restart_after_saves=None)
    async def operation(name,args):
        fixture.calls.append(name);return {'bytes':len(args['data'].encode())}
    fixture.call=operation
    shared=CommonPrimitives(fixture,Usage(),asyncio.Event(),asyncio.Event())
    await shared('bench.save',{'name':'one.txt','encoding':'text','data':'1234'})
    with pytest.raises(RuntimeError,match='before staging'):
        await shared('bench.save',{'name':'two.txt','encoding':'text','data':'12'})
    assert fixture.calls==['bench.save'] and shared.bytes==4


async def test_missing_or_regressed_usage_is_not_zero_cost_success():
    class Backend:
        on_event=None
        async def run(self,ident,*args,**kwargs):return {'usage':{},'turn':{'status':'completed'}}
    backend=Backend();usage=Usage();usage.attach(backend)
    await backend.run('thread')
    assert usage.result()['usage_complete'] is False
    backend.on_event('thread/tokenUsage/updated',{'threadId':'thread','tokenUsage':{'total':{'totalTokens':100}}})
    backend.on_event('thread/tokenUsage/updated',{'threadId':'thread','tokenUsage':{'total':{'totalTokens':10}}})
    assert usage.result()['counter_regressions'] and not usage.result()['usage_complete']


async def test_exact_guessed_output_does_not_pass_source_protocol(tmp_path):
    async with BenchmarkFixture('paragraph',0,tmp_path) as fixture:
        # A test may inspect the private grader fixture; no model or tool can.
        for name,contents in fixture._expected.items():(fixture.output_dir/name).write_bytes(contents)
        grade=fixture.grade()
        assert grade['byte_quality_pass'] is True
        assert grade['protocol_pass'] is False and grade['quality_pass'] is False
