"""Private host-pool acceptance coordinator; no production routes use this module."""
import asyncio
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from ore.config import Settings
from ore.engine import Engine
from ore.execution import create_execution_router
from ore.models import Mission
from ore.policy import AccessDenied
from ore.pool import create_pool_router
from ore.store import LeaseLost
from ore.tools import ToolRuntime

engine = Engine(Settings(state_dir=Path('/tmp/pool-coordinator'), max_workers=0, execution_backend='remote',
    pool_token=os.environ['ORE_POOL_TOKEN'], pool_idle_seconds=0, pool_max_executors=5))
app = FastAPI(); app.include_router(create_pool_router(engine.pool)); app.include_router(create_execution_router(engine.execution))
state = {}

@app.exception_handler(AccessDenied)
async def denied(request, exc): return JSONResponse({'error': str(exc)}, status_code=403)
@app.exception_handler(LeaseLost)
async def lost(request, exc): return JSONResponse({'error': str(exc)}, status_code=409)
@app.get('/fixture/{number}')
async def fixture(number: int):
    await asyncio.sleep(.25)
    return HTMLResponse('<html><body><main>Exact pool fixture ' + str(number) + '</main></body></html>')

@app.post('/test/prepare')
async def prepare():
    base = 'http://pool-coordinator:8765'
    engine.save_profile({'id': 'fixture', 'allow_private_network': True, 'origins': [base],
        'max_browser_sessions': 5, 'max_executor_sessions': 5, 'api_interval': .01})
    mission = Mission(goal='Actual isolated executor pool fixture', urls=[base + '/fixture/0'],
        allowed_origins=[base], access_profile='fixture', budget={'max_agent_workers': 5},
        parallelism={'initial': 5, 'per_origin': 5}, limits={'origin_min_interval_seconds': .01}, artifact_roles=[]).model_dump(mode='json')
    job = engine.store.create_job(mission); state['job'] = job
    for i in range(5): engine.store.create_task(job['id'], 'retrieve', {'index': i}, str(i))
    await engine.scheduler.tick()
    return engine.scheduler.snapshot()['global']

@app.post('/test/state')
async def status():
    await engine.scheduler.tick()
    return {'ready': engine.execution.available(), 'global': engine.scheduler.snapshot()['global']}

@app.post('/test/run')
async def run():
    job = state['job']; claims = [engine.store.claim_task('fixture-' + str(i), lease_seconds=120, job_id=job['id']) for i in range(5)]
    async def one(i, task):
        runtime = ToolRuntime(engine, job['id'], task)
        result = await runtime.execute('fetch', {'url': 'http://pool-coordinator:8765/fixture/' + str(i)})
        return {'index': i, 'status': result.get('status'), 'exact_content': 'Exact pool fixture ' + str(i) in result.get('html', ''), 'error': result.get('error')}
    results = await asyncio.gather(*(one(i, task) for i, task in enumerate(claims)))
    ids = [a['worker_id'] for a in engine.execution._list('assignment')]
    for task in claims:
        await engine.execution.release_task(task)
        engine.store.finish_task(task['id'], task['worker_id'], task['fence'], {'fixture_verified': True}, task['revision'])
    await engine.scheduler.tick()
    return {'results': results, 'worker_ids': ids, 'job_id': job['id'], 'completed': len(claims)}
