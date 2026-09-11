"""Deterministic claim/publication and pre-entry cancellation regressions.

Only SQLite and local async fixture tools are used; no provider or network.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ore.engine import Engine
from ore.models import canonical_digest
from ore.store import Store
from ore.workflow import WorkflowManager


class Registry:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.swallow_cancel = False
    def catalog(self):
        return [{'name': 'fixture.echo', 'version': 'v1',
                 'digest': canonical_digest('fixture.echo.v1'), 'read_only': True, 'replay_safe': True}]
    async def execute(self, name, arguments, runtime):
        self.calls.append(arguments)
        self.entered.set()
        if arguments.get('block'):
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.swallow_cancel:
                    raise
                await self.release.wait()
        return arguments


@pytest.fixture
def engine(tmp_path):
    value = object.__new__(Engine)
    value.store = Store('sqlite:///' + str(tmp_path / 'state.sqlite')); value.store.initialize()
    value.settings = SimpleNamespace(state_dir=tmp_path)
    value.capabilities = Registry()
    value.start = AsyncMock()
    value.profile = lambda mission: {'id': 'public'}
    value.event = lambda job, kind, payload: value.store.append_event(job, kind, payload)
    value.execution = SimpleNamespace(enabled=False)
    value.backend = SimpleNamespace(interrupt=AsyncMock())
    value.workers = {}; value.running = {}; value.claiming = {}; value.loops = []
    value.stopping = False; value.retiring_workers = set()
    value.workflows = WorkflowManager(value)
    yield value
    value.store.close()


async def make_run(engine, inputs=None):
    run = engine.workflows.create_run({'goal': 'Fixture interruption verification', 'artifact_roles': [],
        'workflow': {'nodes': [{'id': 'work', 'kind': 'tool', 'tool': 'fixture.echo',
                               'inputs': inputs or {'value': 3}}]}})
    await engine.workflows.start_run(run['id'])
    return run


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


async def test_pre_entry_cancel_ack_requires_known_local_not_started(engine):
    run = await make_run(engine)
    worker = 'local-fixture'
    task = engine.store.claim_task(worker, job_id=run['job_id'])
    engine.workers[worker] = {'id': worker, 'state': 'idle'}
    if hasattr(engine, '_start_claimed_task'):
        future = engine._start_claimed_task(task, worker)
    else:
        # Frozen Engine.worker_loop publishes exactly this future, with no yield
        # before interrupt can cancel its first scheduled execution step.
        future = asyncio.create_task(engine.execute_task(task, worker))
        engine.running[(task['job_id'], task['id'])] = future
    assert not engine.workflows.active
    stopped = await engine.workflows.interrupt(run['id'])
    await asyncio.gather(future, return_exceptions=True)
    assert engine.capabilities.calls == []
    assert engine.backend.interrupt.await_count == 0
    assert stopped['interrupt_confirmed'] is True
    record = stopped['interruptions'][0]
    assert record['worker_ack_basis'] == 'local_coroutine_not_started'
    await engine.workflows.resume(run['id'])
    fresh = engine.store.claim_task(worker, job_id=run['job_id'])
    await engine.execute_task(fresh, worker)
    assert engine.workflows.get_run(run['id'])['status'] == 'completed'
    assert len(engine.capabilities.calls) == 1


async def test_interrupt_waits_for_claim_to_publish_before_ack(engine):
    run = await make_run(engine)
    claimed, publish = threading.Event(), threading.Event()
    original = engine.store.claim_task
    def delayed_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            claimed.set()
            assert publish.wait(5), 'fixture release was not delivered'
        return result
    engine.store.claim_task = delayed_claim
    loop = asyncio.create_task(engine.worker_loop('local-fixture'))
    controller = None
    try:
        await until(claimed.is_set)
        assert engine.running == {} and engine.workflows.active == {}
        controller = asyncio.create_task(engine.workflows.interrupt(run['id']))
        await asyncio.sleep(.03)
        # The SQL task is claimed, but the local execute coroutine is not yet
        # published. Do not declare stop complete while the claim can dispatch.
        was_waiting = not controller.done()
        publish.set()
        stopped = await controller
        assert was_waiting is True
        assert stopped['interrupt_confirmed'] is True
        assert stopped['status'] == 'paused'
        assert engine.capabilities.calls == []
        assert engine.backend.interrupt.await_count == 0
    finally:
        publish.set()
        if controller and not controller.done(): await controller
        engine.stopping = True
        loop.cancel(); await asyncio.gather(loop, return_exceptions=True)
        await asyncio.gather(*list(engine.claiming.values()), return_exceptions=True)


async def test_unknown_remote_or_unregistered_claim_never_gains_local_ack(engine):
    run = await make_run(engine)
    task = engine.store.claim_task('remote-unreachable', job_id=run['job_id'])
    stopped = await engine.workflows.interrupt(run['id'])
    assert stopped['interrupt_confirmed'] is False
    assert stopped['interruptions'][0]['worker_ack'] is False
    assert engine.backend.interrupt.await_count == 0


async def test_started_tool_that_ignores_cancel_remains_unconfirmed(engine):
    run = await make_run(engine, {'block': True})
    engine.capabilities.swallow_cancel = True
    task = engine.store.claim_task('local-fixture', job_id=run['job_id'])
    engine.workers['local-fixture'] = {'id': 'local-fixture', 'state': 'idle'}
    if hasattr(engine, '_start_claimed_task'):
        future = engine._start_claimed_task(task, 'local-fixture')
    else:
        future = asyncio.create_task(engine.execute_task(task, 'local-fixture'))
        engine.running[(task['job_id'], task['id'])] = future
    try:
        await engine.capabilities.entered.wait()
        stopped = await engine.workflows.interrupt(run['id'])
        assert stopped['interrupt_confirmed'] is False
        assert stopped['interruptions'][0]['worker_ack'] is False
    finally:
        engine.capabilities.release.set()
        await asyncio.gather(future, return_exceptions=True)
    assert engine.workflows.get_run(run['id'])['interrupt_confirmed'] is True


async def test_worker_cancellation_during_claim_does_not_drop_its_result(engine):
    run = await make_run(engine)
    claimed, publish = threading.Event(), threading.Event()
    original = engine.store.claim_task
    def delayed_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            claimed.set()
            assert publish.wait(5)
        return result
    engine.store.claim_task = delayed_claim
    loop = asyncio.create_task(engine.worker_loop('local-fixture'))
    controller = None
    try:
        await until(claimed.is_set)
        controller = asyncio.create_task(engine.workflows.interrupt(run['id']))
        initial = await controller
        # A delayed claim may outlive the bounded control response; it must
        # remain unconfirmed until the local dispatch owner rejects it.
        assert initial['interrupt_confirmed'] is False
        assert initial['interruptions'][0]['worker_ack'] is False
        engine.stopping = True
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        publish.set()
        await controller
        await asyncio.gather(*list(engine.claiming.values()), return_exceptions=True)
        assert engine.workflows.get_run(run['id'])['interrupt_confirmed'] is True
        assert engine.capabilities.calls == []
        assert not engine.running
        assert not engine.claiming
    finally:
        publish.set()
        if controller and not controller.done(): await controller
        loop.cancel(); await asyncio.gather(loop, return_exceptions=True)


async def test_duplicate_interrupts_do_not_ack_an_unpublished_claim(engine):
    run = await make_run(engine)
    claimed, publish = threading.Event(), threading.Event()
    original = engine.store.claim_task
    def delayed_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            claimed.set()
            assert publish.wait(5)
        return result
    engine.store.claim_task = delayed_claim
    loop = asyncio.create_task(engine.worker_loop('local-fixture'))
    first = None
    try:
        await until(claimed.is_set)
        first = asyncio.create_task(engine.workflows.interrupt(run['id']))
        await until(lambda: bool(engine.workflows.get_run(run['id'])['interruptions']))
        repeated = await engine.workflows.interrupt(run['id'])
        assert repeated['interrupt_confirmed'] is False
        assert len(repeated['interruptions']) == 1
        assert repeated['interruptions'][0]['worker_ack'] is False
        publish.set()
        stopped = await first
        assert stopped['interrupt_confirmed'] is True
        assert len(stopped['interruptions']) == 1
        assert engine.capabilities.calls == []
    finally:
        publish.set()
        if first and not first.done(): await first
        engine.stopping = True
        loop.cancel(); await asyncio.gather(loop, return_exceptions=True)
        await asyncio.gather(*list(engine.claiming.values()), return_exceptions=True)


async def test_stop_rejects_claim_returned_before_workflow_interrupt_record(engine):
    run = await make_run(engine)
    claimed, publish = threading.Event(), threading.Event()
    original = engine.store.claim_task
    def delayed_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            claimed.set()
            assert publish.wait(5)
        return result
    engine.store.claim_task = delayed_claim
    worker = asyncio.create_task(engine.worker_loop('local-fixture'))
    engine.loops = [worker]
    async def scheduler_close():
        assert engine.stopping is True
        assert not engine.workflows.get_run(run['id'])['interruptions']
        # Exercise the actual Engine.stop ordering: claim returns while an
        # earlier service closes, before workflows.close creates any record.
        publish.set()
        await until(worker.done)
    engine.scheduler = SimpleNamespace(close=scheduler_close)
    engine.challenge_service = SimpleNamespace(close=AsyncMock())
    engine.conversations = SimpleNamespace(close=AsyncMock())
    engine.execution.close = AsyncMock()
    engine.browser = SimpleNamespace(close=AsyncMock())
    engine.backend.close = AsyncMock()
    try:
        await until(claimed.is_set)
        await engine.stop()
        stopped = engine.workflows.get_run(run['id'])
        assert stopped['status'] == 'paused'
        assert stopped['interrupt_confirmed'] is True
        assert len(stopped['interruptions']) == 1
        assert stopped['interruptions'][0]['worker_ack_basis'] == 'local_coroutine_not_started'
        task = engine.store.tasks(run['job_id'])[0]
        assert task['state'] == 'paused' and task['fence'] == 2
        assert engine.store.attempts(task['id'])[0]['state'] == 'paused'
        assert engine.capabilities.calls == []
        assert not engine.running and not engine.claiming
    finally:
        publish.set()
        worker.cancel(); await asyncio.gather(worker, return_exceptions=True)
        await asyncio.gather(*list(engine.claiming.values()), return_exceptions=True)


async def test_failed_ack_cleans_local_maps_and_retains_proof_for_retry(engine):
    run = await make_run(engine)
    worker = 'local-fixture'
    task = engine.store.claim_task(worker, job_id=run['job_id'])
    engine.workers[worker] = {'id': worker, 'state': 'idle'}
    original = engine.workflows.acknowledge_interrupt
    def unavailable(*args, **kwargs):
        raise OSError('fixture SQL unavailable')
    engine.workflows.acknowledge_interrupt = unavailable
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler(); failures = []
    loop.set_exception_handler(lambda _, context: failures.append(context))
    try:
        future = engine._start_claimed_task(task, worker)
        stopped = await engine.workflows.interrupt(run['id'])
        await asyncio.gather(future, return_exceptions=True)
        assert stopped['interrupt_confirmed'] is False
        assert failures and isinstance(failures[0].get('exception'), OSError)
        assert not engine.running
        assert engine.workers[worker]['state'] == 'idle'
        assert len(engine.not_started_claims) == 1
        engine.workflows.acknowledge_interrupt = original
        stopped = await engine.workflows.interrupt(run['id'])
        assert stopped['interrupt_confirmed'] is True
        assert stopped['interruptions'][0]['worker_ack_basis'] == 'local_coroutine_not_started'
        assert not engine.not_started_claims
        assert engine.capabilities.calls == []
    finally:
        engine.workflows.acknowledge_interrupt = original
        loop.set_exception_handler(previous)
