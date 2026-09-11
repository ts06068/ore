import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from ore.config import SecretStore, Settings
from ore.execution import ExecutionManager
from ore.host_pool import DockerHostPool, HostPoolConfig
from ore.models import Mission
from ore.pool import PoolManager
from ore.policy import AccessDenied
from ore.scheduler import AdaptiveScheduler
from ore.store import Store, tasks


@pytest.fixture
def engine(tmp_path):
    settings = Settings(state_dir=tmp_path, max_workers=8, pool_token='fixture-pool', pool_idle_seconds=0).prepare()
    store = Store(settings.database_url); store.initialize()
    e = SimpleNamespace(settings=settings, store=store, secrets=SecretStore(tmp_path),
        profile=lambda mission: {'id': 'public'}, event=store.append_event)
    e.execution = ExecutionManager(e); e.pool = PoolManager(e); e.scheduler = AdaptiveScheduler(e)
    yield e
    if e.execution.monitor: e.execution.monitor.cancel()
    store.close()


def job_tasks(e, count=10, cap=8, **kwargs):
    mission = Mission(goal='Controlled scheduling', budget={'max_agent_workers': cap}, **kwargs).model_dump(mode='json')
    job = e.store.create_job(mission)
    for i in range(count): e.store.create_task(job['id'], 'retrieve', {}, str(i))
    return job


def succeed_one(e, job):
    task = e.store.claim_task('worker', job_id=job['id'])
    assert task
    e.store.finish_task(task['id'], task['worker_id'], task['fence'], {}, task['revision'])


async def test_initial_five_three_windows_growth_and_failure_backoff_persist_without_revision(engine):
    e = engine; job = job_tasks(e, count=30)
    now = time.time()
    await e.scheduler.tick(now=now)
    assert e.store.get_document('scheduler.job', job['id'])['target'] == 5
    for i in range(3):
        succeed_one(e, job); await e.scheduler.tick(now=now + 15 * (i + 1))
    row = e.store.get_document('scheduler.job', job['id'])
    assert row['target'] == 6 and row['reason'] == 'three_stable_progress_windows'
    task = e.store.claim_task('worker', job_id=job['id'])
    e.store.fail_task(task['id'], task['worker_id'], task['fence'], {'code': 'provider_failure'}, state='failed')
    await e.scheduler.tick(now=now + 60)
    assert e.store.get_document('scheduler.job', job['id'])['target'] == 3
    assert e.store.get_job(job['id'])['revision'] == job['revision']
    restarted = AdaptiveScheduler(e)
    await restarted.tick(now=now + 61)
    assert e.store.get_document('scheduler.job', job['id'])['target'] == 3


async def test_explicit_cap_global_and_origin_limits_are_atomic(engine):
    e = engine; e.settings.scheduler_global_limit = 3
    first = job_tasks(e, cap=2, urls=['https://example.org/a'])
    second = job_tasks(e, cap=8, urls=['https://example.org/b'])
    third = job_tasks(e, cap=8, urls=['https://other.example/a'])
    await e.scheduler.tick()
    assert e.store.get_document('scheduler.job', first['id'])['target'] == 2
    claimed = await asyncio.gather(*(asyncio.to_thread(e.store.claim_task, 'w' + str(i)) for i in range(12)))
    claimed = [t for t in claimed if t]
    assert len(claimed) == 3
    assert sum(t['job_id'] in (first['id'], second['id']) for t in claimed) == 2
    assert sum(t['job_id'] == third['id'] for t in claimed) == 1


async def test_desired_executor_is_not_registered_capacity(engine):
    e = engine; e.execution.enabled = True
    job = job_tasks(e, count=8)
    await e.scheduler.tick()
    row = e.store.get_document('scheduler.control', 'global')
    assert row['desired_executors'] == 5 and row['ready_executors'] == 0
    assert e.store.claim_task('w') is None
    actions = await e.pool.report({'broker_id': 'fixture', 'capacity': 5, 'containers': []})
    assert len(actions['launch']) == 5 and actions['ready'] == 0
    grant = actions['launch'][0]
    with pytest.raises(AccessDenied):
        await e.execution.register(grant['enrollment_token'], {'id': 'wrong'})
    registered = await e.execution.register(grant['enrollment_token'], {'id': grant['worker_id'], 'network_zone': grant['network_zone']})
    with pytest.raises(AccessDenied):
        await e.execution.register(grant['enrollment_token'], {'id': grant['worker_id']})
    await e.scheduler.tick()
    assert e.store.get_document('scheduler.control', 'global')['ready_executors'] == 1
    assert e.store.claim_task('w')
    assert e.store.claim_task('w2') is None
    await e.execution.close()


async def test_drain_never_stops_human_session_or_unacknowledged_command(engine):
    e = engine; e.execution.enabled = True
    job_tasks(e)
    await e.scheduler.tick()
    request = {'broker_id': 'fixture', 'capacity': 3, 'containers': []}
    grants = (await e.pool.report(request))['launch']
    for grant in grants:
        await e.execution.register(grant['enrollment_token'], {'id': grant['worker_id']})
    first, second, third = [g['worker_id'] for g in grants]
    e.execution._put('session', 'human', {'id': 'human', 'worker_id': first, 'closed': False, 'control': 'human'})
    e.execution._put('command', 'waiting', {'id': 'waiting', 'worker_id': second, 'status': 'cancel_requested'})
    e.store.put_document('scheduler.control', 'global', {'desired_executors': 0})
    action = await e.pool.report(request)
    assert action['drain'] == [{'worker_id': third, 'reason': 'idle_above_demand'}]
    assert e.execution._get('worker', third)['state'] == 'draining'
    await e.execution.close()


async def test_host_fixed_image_launch_has_no_privileged_mounts_or_master_secrets(tmp_path):
    calls, captured_env = [], []
    async def docker(*args):
        calls.append(args)
        if args[0] == 'image': return 'sha256:' + 'a' * 64
        if args[0] == 'run':
            path = Path(args[args.index('--env-file') + 1]); captured_env.append(path.read_text())
            return 'b' * 64
        raise AssertionError(args)
    pool = DockerHostPool(HostPoolConfig(server='http://coordinator:8765', network='fixture-network',
        resource_mode='diagnostic_rlimit', state_dir=tmp_path), token='host-role-secret', command=docker)
    grant = {'worker_id': 'pool-fixture-one', 'enrollment_token': 'single-use-fixture', 'network_zone': 'default'}
    await pool.launch(grant)
    run = calls[-1]
    assert '--entrypoint=python3' in run and 'sha256:' + 'a' * 64 in run
    assert '--cgroup-parent=/' in run and any('resource.RLIMIT_AS' in x for x in run)
    assert not any('privileged' in x or 'docker.sock' in x or '.codex' in x or x in ('-v', '--volume') for x in run)
    assert 'host-role-secret' not in captured_env[0] and 'ORE_POOL_TOKEN' not in captured_env[0]
    assert 'single-use-fixture' in captured_env[0]
    assert not list(tmp_path.glob('ore-pool-env-*'))
    assert 'single-use-fixture' not in pool.state_path.read_text()
    with pytest.raises(ValueError):
        DockerHostPool(HostPoolConfig(server='http://x', network='host', state_dir=tmp_path), token='x')


async def test_source_429_and_resource_pressure_reduce_without_changing_rate_reservations(engine):
    e = engine; job = job_tasks(e, urls=['https://example.org/data'])
    now = time.time(); await e.scheduler.tick(now=now)
    e.store.penalize_rate('executor-origin:example.org', 90)
    rate = e.store.list_documents('rate')[0]
    await e.scheduler.tick(now=now + 15)
    control = e.store.get_document('scheduler.job', job['id'])
    assert control['target'] == 2 and control['reason'] == 'source_backoff'
    assert e.store.list_documents('rate')[0]['blocked_until'] == rate['blocked_until']
    await e.scheduler.tick(now=now + 30, observation={'memory_ratio': .95})
    assert e.store.get_document('scheduler.job', job['id'])['target'] == 1


async def test_operator_fixed_target_does_not_exceed_explicit_plan_max(engine):
    e = engine; job = job_tasks(e, cap=3, parallelism={'initial': 5, 'mode': 'fixed'})
    now = time.time(); await e.scheduler.tick(now=now)
    for i in range(4):
        succeed_one(e, job); await e.scheduler.tick(now=now + (i + 1) * 15)
    assert e.store.get_document('scheduler.job', job['id'])['target'] == 3


async def test_api_assignment_slots_do_not_consume_global_profile_browser_slot(engine):
    e = engine; e.execution.enrollment_token = 'fixture-enrollment'
    e.profile = lambda mission: {'id': 'institution', 'max_browser_sessions': 1}
    job = job_tasks(e, cap=2)
    for name in ('executor-a', 'executor-b'):
        await e.execution.register('fixture-enrollment', {'id': name})
    tasks_claimed = [e.store.claim_task('m' + str(i), job_id=job['id']) for i in range(2)]
    assigned = [await e.execution._assignment(t, job['id'], needs_browser=False) for t in tasks_claimed]
    assert len({a['worker_id'] for a in assigned}) == 2
    assert not any(a['browser_reserved'] for a in assigned)
    one = await e.execution._assignment(tasks_claimed[0], job['id'], needs_browser=True)
    assert one['browser_reserved']
    with pytest.raises(AccessDenied, match='browser concurrency'):
        await e.execution._assignment(tasks_claimed[1], job['id'], needs_browser=True)
    await e.execution.close()


async def test_unknown_docker_state_cannot_be_reported_as_absent_or_drained(tmp_path):
    from ore.host_pool import HostPoolError, DockerObjectMissing
    calls = []
    async def unavailable(*args):
        calls.append(args); raise HostPoolError('daemon_unreachable')
    pool = DockerHostPool(HostPoolConfig(server='http://fixture', state_dir=tmp_path, resource_mode='diagnostic_rlimit'), token='fixture', command=unavailable)
    pool.containers['pool-one'] = {'name': 'owned', 'id': 'a' * 64}
    assert (await pool.inventory())[0]['state'] == 'unknown'
    with pytest.raises(HostPoolError, match='unconfirmed'): await pool.drain('pool-one')
    assert not any(c[0] in ('stop', 'rm') for c in calls)
    async def absent(*args): raise DockerObjectMissing('container_absent')
    pool.command = absent
    assert (await pool.inventory())[0]['state'] == 'absent'


async def test_native_watchdog_capacity_is_one_even_when_desired_is_five(engine):
    e = engine; e.settings.browser_backend = 'desktop_chrome'; e.settings.desktop_resource_mode = 'watchdog'
    job_tasks(e)
    await e.scheduler.tick()
    assert e.scheduler.snapshot()['global']['native_desktop_limit'] == 1
    assert e.store.claim_task('first')
    assert e.store.claim_task('second') is None
