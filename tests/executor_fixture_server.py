"""Private Docker acceptance coordinator and HTTP fixture; never used in production."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ore.config import Settings
from ore.engine import Engine
from ore.execution import create_execution_router
from ore.policy import AccessDenied
from ore.store import LeaseLost
from ore.tools import ToolRuntime

engine = Engine(Settings(state_dir=Path(os.environ.get('ORE_FIXTURE_STATE_DIR', '/tmp/fixture-coordinator')), max_workers=0,
    execution_backend='remote', executor_enrollment_token=os.environ['ORE_EXECUTOR_ENROLLMENT_TOKEN']))
app = FastAPI()
app.include_router(create_execution_router(engine.execution))

@app.exception_handler(AccessDenied)
async def denied(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=403)

@app.exception_handler(LeaseLost)
async def lost(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=409)

@app.get('/healthz')
async def health():
    return {'ok': True, 'available_executors': engine.execution.available()}

@app.get('/fixture/{ident}')
async def fixture(ident: str):
    response = HTMLResponse('<html><title>Executor fixture</title><main id="target">Exact isolated paragraph ' + ident + '.</main></html>')
    response.set_cookie('fixture_owner', ident)
    return response

@app.get('/artifact/{ident}')
async def artifact(ident: str, request: Request):
    return Response('artifact-' + ident + ':cookie=' + request.cookies.get('fixture_owner', 'missing'), media_type='text/plain')

@app.post('/test/parallel')
async def parallel():
    engine.save_profile({'id': 'fixture', 'name': 'Controlled local fixture', 'allow_private_network': True,
        'persist_session': False, 'max_browser_sessions': 2, 'api_interval': 0,
        'origins': ['http://coordinator.ore.test:8765']})
    results = []
    async def one(ident):
        mission = {'goal': 'Retrieve controlled fixture ' + ident, 'urls': ['http://coordinator.ore.test:8765/fixture/' + ident],
            'allowed_origins': ['http://coordinator.ore.test:8765'], 'access_profile': 'fixture',
            'artifact_roles': ['excerpt', 'attachment'], 'limits': {'origin_min_interval_seconds': 0},
            'budget': {'max_agent_workers': 2, 'max_turns': 12}}
        job = engine.store.create_job(mission)
        task = engine.store.create_task(job['id'], 'retrieve', {}, 'root')
        task = engine.store.claim_task('controlled-agent-' + ident, 600, job['id'])
        runtime = ToolRuntime(engine, job['id'], task)
        resource = await runtime.execute('resource', {'id': 'resource-' + ident, 'title': 'Fixture ' + ident,
            'url': mission['urls'][0], 'classification': 'included'})
        page = await runtime.execute('browser_open', {'url': mission['urls'][0]})
        excerpt = await runtime.execute('page_extract', {'session_id': page['session_id'], 'epoch': page['epoch'],
            'selector': '#target', 'resource_id': resource['id']})
        attachment = await runtime.execute('download', {'url': 'http://coordinator.ore.test:8765/artifact/' + ident,
            'session_id': page['session_id'], 'resource_id': resource['id'], 'role': 'attachment'})
        expected = ('artifact-' + ident + ':cookie=' + ident).encode()
        saved = Path(attachment['path']).read_bytes()
        assert saved == expected, 'Browser cookie crossed executor boundaries or was lost'
        assert attachment['sha256'] == hashlib.sha256(expected).hexdigest()
        assert attachment.get('validator') == 'isolated_service'
        assert excerpt['artifact'].get('validator') == 'isolated_service'
        assert excerpt['text'] == ['Exact isolated paragraph ' + ident + '.']
        session = engine.execution.get_session(page['session_id'])
        assert session.worker_id == attachment['executor_id']
        result = {'job_id': job['id'], 'task_id': task['id'], 'executor_id': session.worker_id,
            'session_id': session.id, 'excerpt_sha256': excerpt['artifact']['sha256'],
            'download_sha256': attachment['sha256'], 'bytes': attachment['bytes'], 'cookie_isolated': True, 'validator': attachment.get('validator'),
            'upload_receipt_id': attachment['upload_receipt_id']}
        results.append(result)
        return task
    tasks = await asyncio.gather(one('A'), one('B'))
    assert len({item['executor_id'] for item in results}) == 2
    assert len({item['session_id'] for item in results}) == 2
    # Handoff must retain a real remote browser after task settlement.
    session = results[0]['session_id']
    before = await engine.execution.session_summary(session)
    handoff = await engine.execution.browser_command(session, 'takeover', {})
    assert handoff['control'] == 'human' and handoff['epoch'] > before['epoch']
    task = next(t for t in tasks if t['id'] == results[0]['task_id'])
    engine.store.fail_task(task['id'], task['worker_id'], task['fence'], {'code': 'human_fixture'}, task['revision'], state='awaiting_user')
    await engine.execution.release_task(task)
    human = await engine.execution.browser_command(session, 'action', {'action': 'scroll', 'epoch': handoff['epoch'], 'deltaY': 50, 'owner': 'human'})
    assert human['control'] == 'human'
    report = {'passed': True, 'actual_executor_containers': len({r['executor_id'] for r in results}),
        'model_calls': 0, 'coordinator_browser_started': engine.browser.browser is not None,
        'artifacts': results, 'human_session_retained_after_task_settlement': True,
        'worker_runtime': [dict(id=r['id'], **r['runtime']) for r in engine.execution._list('worker')]}
    engine.store.put_document('execution.acceptance', 'latest', report)
    Path('/tmp/executor-acceptance.json').write_text(json.dumps(report, indent=2))
    return report

@app.post('/test/recover')
async def recover():
    report = engine.store.get_document('execution.acceptance', 'latest')
    row = report['artifacts'][0]
    sid = row['session_id']
    for _ in range(30):
        session = engine.execution.get_session(sid)
        if session.frame:
            break
        await asyncio.sleep(0.2)
    assert session.frame, 'Existing executor frame did not reconnect'
    assert session.control == 'human'
    human = await engine.execution.browser_command(sid, 'action', {'action': 'scroll', 'epoch': session.epoch, 'deltaY': -50, 'owner': 'human'})
    assert human['control'] == 'human'
    await engine.execution.browser_command(sid, 'resume', {})
    engine.store.reset_for_resume(row['job_id'])
    replacement = engine.store.claim_task('replacement-model', 120, row['job_id'])
    assert replacement and replacement['fence'] > 1
    runtime = ToolRuntime(engine, row['job_id'], replacement)
    observed = await runtime.execute('browser_observe', {'session_id': sid})
    assert observed['session_id'] == sid and 'Exact isolated paragraph' in observed['text']
    assert engine.execution.get_session(sid).worker_id == row['executor_id']
    report.update(coordinator_restart_reconnected_same_browser=True, newer_task_fence_reused_browser=True)
    engine.store.put_document('execution.acceptance', 'latest', report)
    return report


@app.post('/test/prepare-recreate')
async def prepare_recreate():
    report = engine.store.get_document('execution.acceptance', 'latest')
    item = report['artifacts'][0]
    task = engine.store.get_task(item['task_id'])
    await engine.execution.browser_command(item['session_id'], 'takeover', {})
    engine.store.fail_task(task['id'], task['worker_id'], task['fence'], {'code': 'executor_loss_fixture'}, task['revision'], state='awaiting_user')
    report['before_recreate'] = {'session_id': item['session_id'], 'worker_id': item['executor_id'],
        'boot_id': engine.execution.get_session(item['session_id']).metadata['boot_id'], 'fence': task['fence']}
    engine.store.put_document('execution.acceptance', 'latest', report)
    return report['before_recreate']


@app.post('/test/recreate')
async def recreate():
    report = engine.store.get_document('execution.acceptance', 'latest')
    item, prior = report['artifacts'][0], report['before_recreate']
    for _ in range(40):
        worker = engine.execution._get('worker', prior['worker_id'])
        if worker and worker['boot_id'] != prior['boot_id'] and worker['state'] == 'idle':
            break
        await asyncio.sleep(0.2)
    assert engine.execution.get_session(prior['session_id']).closed, 'Lost executor browser was not invalidated'
    job = engine.store.get_job(item['job_id'])
    created = await engine.execution.create_session(job['id'], job['mission'], engine.profile(job['mission']), task_id=item['task_id'])
    sid = created['id']
    assert sid != prior['session_id']
    handoff = await engine.execution.browser_command(sid, 'takeover', {})
    page = await engine.execution.browser_command(sid, 'navigate', {'url': job['mission']['urls'][0],
        'epoch': handoff['epoch'], 'owner': 'human', 'screenshot': False})
    assert page['session_id'] == sid and page['control'] == 'human'
    await engine.execution.browser_command(sid, 'resume', {})
    engine.store.reset_for_resume(job['id'])
    claim = engine.store.claim_task('model-after-executor-loss', 120, job['id'])
    assert claim['fence'] > prior['fence']
    owned = await engine.execution.task_sessions(job['id'], claim['id'])
    assert [row['id'] for row in owned] == [sid]
    runtime = ToolRuntime(engine, job['id'], claim)
    observed = await runtime.execute('browser_observe', {'session_id': sid})
    assert observed['session_id'] == sid and 'Exact isolated paragraph' in observed['text']
    current = engine.execution.get_session(sid)
    assert current.worker_id == prior['worker_id'] and current.metadata['boot_id'] != prior['boot_id']
    report.update(executor_process_loss_recreated_browser=True, recreated_browser_adopted_by_original_task=True,
        recreated_session_id=sid, recreated_task_fence=claim['fence'])
    engine.store.put_document('execution.acceptance', 'latest', report)
    return report
