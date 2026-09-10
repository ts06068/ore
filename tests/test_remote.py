import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import ore.remote as remote
import ore.worker_api as worker_api
from ore.engine import Engine
from ore.policy import AccessDenied, ModelPolicy
from ore.store import LeaseLost, Store


def catalog(name):
    return [{'model': name, 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}]


class FakeEngine:
    check_egress = Engine.check_egress
    model_observation = Engine.model_observation

    def __init__(self, store):
        self.store, self.workers, self.catalog = store, {}, catalog('local-model')
        self.routes = []

    def routing_for(self, job, kind, failures=0, catalog=None):
        self.routes.append((job['id'], kind, catalog))
        return ModelPolicy(catalog).choose(job['mission'], kind, failures, validated=True)

    def event(self, job_id, kind, payload):
        return self.store.append_event(job_id, kind, payload)

    def reconcile(self, job_id):
        pass


class FakeRuntime:
    def __init__(self, engine, job_id, task):
        self.engine, self.job_id, self.task = engine, job_id, task
        self.last_image = 'data:image/png;base64,private-image'

    async def execute(self, name, args):
        return {'title': 'Archive', 'text': 'private-page-body', 'nested': {'body': 'secret-body'},
                'fence': self.task['fence'], 'audit': {'status': 'complete_within_scope'}}


def application(engine):
    app = FastAPI()
    app.include_router(worker_api.create_worker_router(engine))

    @app.exception_handler(LeaseLost)
    async def lease_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(AccessDenied)
    async def denied(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=403)

    return app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 'state.sqlite'}")
    store.initialize()
    engine = FakeEngine(store)
    monkeypatch.setattr(worker_api, 'ToolRuntime', FakeRuntime)
    yield engine, application(engine)
    store.close()


def job_with_task(engine, model='model-a', **fields):
    mission = {'goal': 'Collect fixture', 'model': model, 'effort': 'high', **fields}
    job = engine.store.create_job(mission)
    engine.store.create_task(job['id'], 'plan', {}, 'root')
    return job


@pytest.mark.asyncio
async def test_catalog_per_worker_and_coordinator_restart(setup):
    engine, app = setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for name in ('a', 'b'):
            assert (await client.post('/v1/workers/register', json={'id': name, 'models': catalog('model-' + name)})).status_code == 200
        assert engine.catalog == catalog('local-model')
    # Recreate coordinator memory: registered catalogs remain available in Store.
    engine = FakeEngine(engine.store)
    job_with_task(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(engine)), base_url='http://test') as client:
        assert (await client.post('/v1/workers/b/claim')).json()['task'] is None
        offer = (await client.post('/v1/workers/a/claim')).json()
        assert offer['task']
        task = offer['task']
        route = await client.post(f"/v1/workers/a/tasks/{task['id']}/route", json={'fence': task['fence']})
        assert route.json()['model'] == 'model-a'
        assert engine.routes[-1][2] == catalog('model-a')
        assert engine.catalog == catalog('local-model')


@pytest.mark.asyncio
async def test_pause_rejects_heartbeat_without_renewal_and_accepts_settlement(setup):
    engine, app = setup
    job = job_with_task(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        await client.post('/v1/workers/register', json={'id': 'a', 'models': catalog('model-a')})
        task = (await client.post('/v1/workers/a/claim')).json()['task']
        prefix = f"/v1/workers/a/tasks/{task['id']}"
        lease_before = engine.store.get_task(task['id'])['lease_expires_at']
        engine.store.update_job(job['id'], status='paused')
        response = await client.post(prefix + '/heartbeat', json={'fence': task['fence']})
        assert response.status_code == 409
        assert engine.store.get_task(task['id'])['lease_expires_at'] == lease_before
        response = await client.post(prefix + '/fail', json={'fence': task['fence'], 'error': {'code': 'interrupted'}})
        assert response.status_code == 200
        assert response.json()['state'] == 'paused'
        assert engine.store.get_worker('a')['state'] == 'idle'


@pytest.mark.asyncio
async def test_claim_egress_and_metadata_observations(setup):
    engine, app = setup
    forbidden = job_with_task(engine, external_model_content='none')
    permitted = job_with_task(engine, external_model_content='metadata')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        await client.post('/v1/workers/register', json={'id': 'a', 'models': catalog('model-a')})
        response = await client.post('/v1/workers/a/claim')
        offer = response.json()
        assert offer['job']['id'] == permitted['id']
        assert 'private-page-body' not in response.text
        assert engine.store.tasks(forbidden['id'])[0]['state'] == 'queued'
        task = offer['task']
        response = await client.post(f"/v1/workers/a/tasks/{task['id']}/tool",
            json={'fence': task['fence'], 'tool': 'state', 'arguments': {}})
        assert response.status_code == 200
        assert 'private-page-body' not in response.text and 'secret-body' not in response.text
        assert 'image_url' not in response.json()
        assert response.json()['result']['title'] == 'Archive'


@pytest.mark.asyncio
async def test_remote_pause_interrupts_actual_inflight_backend_call(setup, monkeypatch):
    engine, app = setup
    job = job_with_task(engine)
    entered, interrupted, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Backend:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def models(self):
            return catalog('model-a')
        async def thread(self, *args, **kwargs):
            return 'fixture-thread'
        async def run(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        async def interrupt(self, thread):
            assert thread == 'fixture-thread'
            interrupted.set()

    original_client = httpx.AsyncClient
    monkeypatch.setattr(remote, 'CodexBackend', lambda *args: Backend())
    monkeypatch.setattr(remote, 'HEARTBEAT_SECONDS', 0.02)
    monkeypatch.setattr(remote.httpx, 'AsyncClient', lambda **kwargs:
        original_client(**kwargs, transport=httpx.ASGITransport(app=app)))
    running = asyncio.create_task(remote.worker('http://test', 'fixture-token', once=True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        engine.store.update_job(job['id'], status='awaiting_user')
        await asyncio.wait_for(running, 2)
        assert interrupted.is_set() and cancelled.is_set()
        assert engine.store.tasks(job['id'])[0]['state'] == 'awaiting_user'
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_remote_decision_usage_observed_before_action(setup):
    engine, app = setup
    job = job_with_task(engine, budget={'max_tokens': 20, 'max_turns': 5})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        await client.post('/v1/workers/register', json={'id': 'a', 'models': catalog('model-a')})
        task = (await client.post('/v1/workers/a/claim')).json()['task']
        response = await client.post(f"/v1/workers/a/tasks/{task['id']}/decision", json={
            'fence': task['fence'], 'usage': {'totalTokens': 12}, 'decision': {'tool': 'state'}, 'thread_id': 'one'})
        assert response.status_code == 200
        assert engine.store.get_budget(f"{job['id']}:1:1", 'model_tokens')['used'] == 12
        response = await client.post(f"/v1/workers/a/tasks/{task['id']}/decision", json={
            'fence': task['fence'], 'usage': {'totalTokens': 10}})
        assert response.status_code == 403
        assert any(event['type'] == 'agent_decision' for event in engine.store.events(job['id']))
