"""Concurrent browser contexts preserve profile changes without restoring deletions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ore.browser import BrowserManager, merge_browser_profile, merge_storage_state
from ore.config import SecretStore
from ore.executor import ScopedSecrets
from ore.models import canonical_digest
from test_executor import setup, assigned

EMPTY = {'cookies': [], 'origins': []}
REF = 'browser-profile:fixture'


def cookie(domain='one.example', value='fixture-one', **extra):
    return {'name': 'session', 'domain': domain, 'path': '/', 'value': value,
            'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax', **extra}


def state(cookies=(), storage=None):
    return {'cookies': list(cookies), 'origins': [{'origin': origin,
        'localStorage': [{'name': name, 'value': value} for name, value in values.items()]}
        for origin, values in (storage or {}).items()]}


def local_values(value):
    return {row['origin']: {item['name']: item['value'] for item in row['localStorage']} for row in value['origins']}


def test_disjoint_origin_and_local_storage_changes_survive():
    baseline = state([cookie()], {'https://one.example': {'original': 'fixture-original'}})
    earlier = state([cookie(), cookie('two.example')],
        {'https://one.example': {'original': 'fixture-original', 'first': 'fixture-first'},
         'https://two.example': {'two': 'fixture-two'}})
    later = state([cookie(value='fixture-refreshed')],
        {'https://one.example': {'original': 'fixture-original', 'second': 'fixture-second'}})
    merged, report = merge_storage_state(baseline, later, earlier)
    assert {row['domain'] for row in merged['cookies']} == {'one.example', 'two.example'}
    assert next(row for row in merged['cookies'] if row['domain'] == 'one.example')['value'] == 'fixture-refreshed'
    assert local_values(merged)['https://one.example'] == {
        'original': 'fixture-original', 'first': 'fixture-first', 'second': 'fixture-second'}
    assert local_values(merged)['https://two.example'] == {'two': 'fixture-two'}
    assert report['conflict_count'] == 0


def test_deletions_are_not_restored_by_stale_context_or_conflicting_refresh():
    baseline = state([cookie()], {'https://one.example': {'login': 'fixture-original'}})
    unchanged, report = merge_storage_state(baseline, baseline, EMPTY)
    assert unchanged == EMPTY and report['conflict_count'] == 0
    changed = state([cookie(value='fixture-stale-refresh')], {'https://one.example': {'login': 'fixture-stale'}})
    merged, report = merge_storage_state(baseline, changed, EMPTY)
    assert merged == EMPTY and report['conflict_count'] == 2
    merged, report = merge_storage_state(baseline, EMPTY, changed)
    assert merged == EMPTY and report['conflict_count'] == 2


def test_concurrent_same_key_updates_keep_latest_and_report_no_values():
    baseline = state([cookie()], {'https://one.example': {'login': 'fixture-original'}})
    latest = state([cookie(value='fixture-newest')], {'https://one.example': {'login': 'fixture-newest'}})
    current = state([cookie(value='fixture-stale')], {'https://one.example': {'login': 'fixture-stale'}})
    merged, report = merge_storage_state(baseline, current, latest)
    assert merged == latest
    assert report['status'] == 'saved_with_conflicts' and report['conflict_count'] == 2
    assert 'fixture-' not in json.dumps(report) and 'one.example' not in json.dumps(report)


def test_partitioned_cookie_identities_remain_distinct():
    first = state([cookie(partitionKey='https://first.example')])
    second = state([cookie(partitionKey='https://second.example')])
    merged, report = merge_storage_state(EMPTY, second, first)
    assert len(merged['cookies']) == 2 and report['conflict_count'] == 0


def test_separate_secret_store_instances_merge_atomically(tmp_path):
    first_store = SecretStore(tmp_path)
    second_store = SecretStore(tmp_path)
    first_store.set('unrelated-credential', 'fixture-unrelated')
    barrier = threading.Barrier(2)
    def save(secrets, host):
        barrier.wait(timeout=3)
        return merge_browser_profile(secrets, REF, EMPTY, state([cookie(host)]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, store, host) for store, host in
                   [(first_store, 'one.example'), (second_store, 'two.example')]]
        reports = [future.result(timeout=5) for future in futures]
    merged = json.loads(first_store.get(REF))
    assert {row['domain'] for row in merged['cookies']} == {'one.example', 'two.example'}
    assert all(report['conflict_count'] == 0 for report in reports)
    assert first_store.get('unrelated-credential') == 'fixture-unrelated'
    encrypted = first_store.path.read_bytes()
    assert b'fixture-one' not in encrypted and b'fixture-unrelated' not in encrypted


@pytest.mark.asyncio
async def test_save_profile_keeps_local_baseline_and_reports_conflict_without_resurrection(tmp_path):
    manager = object.__new__(BrowserManager)
    manager.profile_lock = asyncio.Lock()
    manager.secrets = SecretStore(tmp_path)
    manager.on_event = None
    manager.sessions = {}
    original = state([cookie()])
    manager.secrets.set(REF, json.dumps(original))
    current = deepcopy(original)
    session = SimpleNamespace(persist_profile=True, profile_ref=REF, profile_baseline=deepcopy(original),
        profile_save_status={}, context=SimpleNamespace(storage_state=AsyncMock(side_effect=lambda: deepcopy(current))),
        job_id='fixture-job', id='fixture-session')
    merge_browser_profile(manager.secrets, REF, original, EMPTY)
    await manager.save_profile(session)
    assert session.profile_baseline == original
    assert json.loads(manager.secrets.get(REF)) == EMPTY
    await manager.save_profile(session)
    assert json.loads(manager.secrets.get(REF)) == EMPTY
    current['cookies'][0]['value'] = 'fixture-stale-refresh'
    report = await manager.save_profile(session)
    assert report['conflict_count'] == 1 and session.profile_save_status == report
    assert json.loads(manager.secrets.get(REF)) == EMPTY
    assert 'fixture-stale-refresh' not in json.dumps(report)


def test_scoped_secret_adapter_uses_single_merge_rpc():
    calls = []
    context = SimpleNamespace(rpc=lambda *args: calls.append(args) or {'saved': True, 'conflict_count': 0})
    report = ScopedSecrets(context).merge_browser_profile(REF, EMPTY, EMPTY)
    assert calls == [('secret.merge_browser_profile', REF, EMPTY, EMPTY)]
    assert report['saved']


@pytest.mark.asyncio
async def test_executor_profile_merge_is_atomic_scoped_and_returns_no_storage_values(setup):
    engine, manager, app = setup
    grant, job, task, assignment, envelope = await assigned(engine, manager)
    ref = 'browser-profile:' + canonical_digest(['operator', 'public'])
    baseline = state([cookie()])
    engine.secrets.set(ref, json.dumps(baseline))
    latest = state([cookie(), cookie('two.example')])
    merge_browser_profile(engine.secrets, ref, baseline, latest)
    current = state([cookie(value='fixture-refresh')])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
        headers={'authorization': 'Bearer ' + grant['token']}) as client:
        async def rpc(target, before=baseline, after=current, **changed):
            payload = {**envelope, 'method': 'secret.merge_browser_profile', 'args': [target, before, after], **changed}
            return await client.post('/v1/execution/workers/executor-one/rpc', json=payload)
        response = await rpc(ref)
        assert response.status_code == 200 and response.json()['result']['conflict_count'] == 0
        assert 'fixture-refresh' not in response.text and 'cookies' not in response.text
        assert {row['domain'] for row in json.loads(engine.secrets.get(ref))['cookies']} == {'one.example', 'two.example'}
        denied = await rpc('browser-profile:another-profile')
        assert denied.status_code == 403
        denied = await rpc('allowed-key')
        assert denied.status_code == 403
        denied = await rpc(ref, kwargs={'overwrite': True})
        assert denied.status_code == 403
        denied = await rpc(ref, args=[])
        assert denied.status_code == 403
        denied = await rpc(ref, capability='fixture-wrong-capability')
        assert denied.status_code == 403
        assert 'fixture-refresh' not in denied.text
    await manager.close()
