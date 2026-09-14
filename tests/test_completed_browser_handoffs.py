"""An ended browser is not a new user gate for a finished workflow step."""
import pytest
from ore.handoffs import HandoffService
from test_workflow import engine, node, one, plan


async def started(engine, incomplete=False):
    checks = [{'op': 'eq', 'left': {'$ref': 'output.collect.complete'}, 'right': True}] if incomplete else []
    run = engine.workflows.create_run(plan([node('collect', {'complete': False})], acceptance=checks))
    await engine.workflows.start_run(run['id'])
    task = engine.store.tasks(run['job_id'])[0]
    return run, task, HandoffService(engine.store)


@pytest.mark.parametrize('incomplete', [False, True])
async def test_late_browser_loss_does_not_create_a_gate_after_node_completion(engine, incomplete):
    run, task, service = await started(engine, incomplete)
    await one(engine, run)
    before = engine.store.get_document('workflow.run', run['id'])
    assert before['status'] == ('needs_replan' if incomplete else 'completed')
    result = service.record_event(run['job_id'], 'browser_handoff', {
        'session_id': 'ended-desktop', 'task_id': task['id'], 'url': 'https://journal.test/archive'})
    service.record_event(run['job_id'], 'browser_session_lost', {'session_id': 'ended-desktop'})
    assert result is None and service.list(run['job_id'], 'active') == []
    assert engine.store.get_document('workflow.run', run['id']) == before
    assert engine.store.events(run['job_id'])[-1]['type'] == 'handoff.completed_browser_ignored'


@pytest.mark.parametrize('event', ['browser_session_lost', 'executor.session_lost', 'restart'])
async def test_existing_obsolete_request_is_retired_but_history_and_job_failure_remain(engine, event):
    run, task, service = await started(engine, incomplete=True)
    row = service.create(run['job_id'], 'browser', 'Inspect browser', session_id='desktop', task_id=task['id'])
    await one(engine, run)
    if event == 'restart':
        HandoffService(engine.store).recover_interrupted_actions()
    else:
        service.record_event(run['job_id'], event, {'session_id': 'desktop'})
    retired = service.get(row['id'])
    assert retired['status'] == 'cancelled' and retired['resolution'] == 'browser_owner_completed'
    assert retired['receipts'] == row['receipts'] and service.list(run['job_id'], 'active') == []
    assert engine.workflows.get_run(run['id'])['status'] == 'needs_replan'
    version = retired['state_version']
    service.recover_interrupted_actions()
    assert service.get(row['id'])['state_version'] == version


async def test_current_unfinished_browser_still_requests_user_recovery(engine):
    run, task, service = await started(engine)
    row = service.record_event(run['job_id'], 'browser_handoff', {
        'session_id': 'desktop', 'task_id': task['id'], 'url': 'https://journal.test/archive'})
    service.record_event(run['job_id'], 'browser_session_lost', {'session_id': 'desktop'})
    service.recover_interrupted_actions()
    current = service.get(row['id'])
    assert current['status'] == 'needs_user' and current['session_state'] == 'lost'
    assert service.list(run['job_id'], 'active')


@pytest.mark.parametrize('guard', ['source_approval', 'inflight_action', 'different_job'])
async def test_completed_node_does_not_cancel_unrelated_authority(engine, guard):
    run, task, service = await started(engine)
    other, _, _ = await started(engine)
    row = service.create(other['job_id'] if guard == 'different_job' else run['job_id'], 'browser', 'Explicit request',
        session_id='desktop', task_id=task['id'], source='wos' if guard == 'source_approval' else None)
    if guard == 'inflight_action':
        row, _ = service.begin_action(row['id'], 'resume', row['state_version'], 'operator-request')
    await one(engine, run)
    service.recover_interrupted_actions()
    assert service.get(row['id'])['status'] == 'needs_user'
