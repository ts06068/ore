from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ore.models import Mission, Rune
from ore.store import ControlConflict, LeaseLost, Store
import ore.store as store_module


@pytest.fixture
def store(tmp_path):
    value = Store(f"sqlite:///{tmp_path / 'state.sqlite'}")
    value.initialize()
    yield value
    value.close()


def test_mission_ui_aliases_and_contract_hash():
    mission = Mission.model_validate({"name": "Corpus", "instructions": "Collect papers",
        "routing": {"mode": "fixed", "model": "example", "effort": "high"},
        "limits": {"max_agent_workers": 2}, "publication_window": {"from": "2006-01-01", "until_exclusive": "2026-01-01"}})
    assert mission.goal == "Collect papers"
    assert mission.model == "example"
    assert mission.budget.max_agent_workers == 2
    assert Rune(instructions="one").digest != Rune(instructions="two").digest
    with pytest.raises(ValidationError):
        Mission(goal="x", publication_window={"from": "2026-01-01", "until_exclusive": "2025-01-01"})


def test_preserves_mission_fields_and_events(store):
    raw = {"instructions": "Collect", "custom": {"values": [1, "a"]}}
    job = store.create_job(raw, {"instructions": "Observe"})
    assert store.get_job(job["id"])["mission"] == raw
    store.update_job(job["id"], status="running", thread_id="thread-1")
    assert store.get_job(job["id"])["thread_id"] == "thread-1"
    assert store.get_job(job["id"])["status"] == "running"
    first = store.events(job["id"])[-1]["id"]
    event = store.append_event(job["id"], "observed", {"page": 2})
    assert store.events(job["id"], after=first) == [event]


def test_lease_fences_across_store_instances_and_expiry(store, monkeypatch):
    job = store.create_job({"goal": "x"})
    task = store.create_task(job["id"], "download", {"url": "https://a/x"}, "x")
    assert store.create_task(job["id"], "download", {"url": "https://a/x"}, "x")["id"] == task["id"]
    with pytest.raises(ValueError):
        store.create_task(job["id"], "download", {"url": "https://a/other"}, "x")
    stores = [Store(store.database_url) for _ in range(6)]
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            claims = list(executor.map(lambda pair: pair[1].claim_task(f"worker-{pair[0]}", lease_seconds=10), enumerate(stores)))
        successful = [item for item in claims if item]
        assert len(successful) == 1
        first = successful[0]
        future = datetime.now(timezone.utc) + timedelta(seconds=11)
        monkeypatch.setattr(store_module, "utcnow", lambda: future)
        second = stores[1].claim_task("replacement", lease_seconds=60)
        assert second["fence"] == first["fence"] + 1
        with pytest.raises(LeaseLost):
            store.finish_task(task["id"], first["worker_id"], first["fence"], {}, 1)
        store.finish_task(task["id"], "replacement", second["fence"], {"ok": True}, 1)
        assert [attempt["state"] for attempt in store.attempts(task["id"])] == ["abandoned", "succeeded"]
    finally:
        for item in stores:
            item.close()


def test_revision_revokes_old_task_and_refresh_retains_provenance(store):
    job = store.create_job({"goal": "old"})
    resource = store.upsert_resource(job["id"], {"id": "article-1", "doi": "https://doi.org/10.1234/ONE"})
    assert resource["id"] == "article-1"
    assert store.upsert_resource(job["id"], {"doi": "10.1234/one", "title": "Updated"})["id"] == "article-1"
    store.add_artifact(job["id"], {"resource_id": resource["id"], "role": "main_pdf", "sha256": "a" * 64})
    store.create_task(job["id"], "agent", {}, "agent")
    claimed = store.claim_task("worker")
    changed = store.revise_job(job["id"], {"goal": "new"})
    assert changed["revision"] == 2
    with pytest.raises(LeaseLost):
        store.finish_task(claimed["id"], "worker", claimed["fence"], {}, 1)
    store.refresh_job(job["id"])
    assert store.artifacts(job["id"]) == []
    assert len(store.artifacts(job["id"], all_generations=True)) == 1
    assert len(store.observations(job["id"])) >= 3


def test_discovery_cursor_fanout_transaction_rolls_back(store):
    job = store.create_job({"goal": "x"})
    with pytest.raises(KeyError):
        store.commit_discovery(job["id"], "archive", "page-2", [{"id": "first"}],
            [{"kind": "fetch", "input": {}, "key": "first"}, {"kind": "fetch", "input": {}}])
    assert store.resources(job["id"]) == []
    assert store.tasks(job["id"]) == []
    assert store.get_checkpoint(job["id"], "archive") is None
    store.commit_discovery(job["id"], "archive", "page-2", [{"id": "first"}],
        [{"kind": "fetch", "input": {}, "key": "first"}])
    assert store.get_checkpoint(job["id"], "archive") == "page-2"
    assert len(store.tasks(job["id"])) == 1


def test_challenge_budget_shared_across_jobs_and_resume(store):
    jobs = [store.create_job({"goal": "x"}) for _ in range(2)]
    for number in range(3):
        reservation = store.reserve_challenge(jobs[number % 2]["id"], "https://source", "same-account")
        assert reservation["allowed"]
        duplicate = store.reserve_challenge(jobs[0]["id"], "https://source", "same-account")
        assert duplicate["reason"] == "attempt_in_flight"
        store.finish_challenge(reservation["id"], reservation["token"], active_seconds=1)
    store.reset_for_resume(jobs[0]["id"])
    denied = store.reserve_challenge(jobs[0]["id"], "https://source", "same-account", max_attempts=999)
    assert not denied["allowed"]
    assert denied["attempts"] == 3
    store.extend_challenge_budget(denied["id"], 1, 10, "explicit-user-action")
    assert store.reserve_challenge(jobs[0]["id"], "https://source", "same-account")["allowed"]


def test_browser_control_epoch_survives_handoff(store):
    job = store.create_job({"goal": "x"})
    agent = store.acquire_control(job["id"], "browser", "agent-1")
    request = store.request_control(job["id"], "browser", "user-1")
    with pytest.raises(ControlConflict):
        store.validate_control(job["id"], "browser", "agent-1", agent["epoch"])
    human = store.acquire_control(job["id"], "browser", "user-1", "user", request["epoch"])
    with pytest.raises(ControlConflict):
        store.acquire_control(job["id"], "browser", "agent-1", expected_epoch=human["epoch"])
    paused = store.release_control(job["id"], "browser", "user-1", human["epoch"])
    resumed = store.acquire_control(job["id"], "browser", "agent-1", expected_epoch=paused["epoch"])
    assert resumed["epoch"] > human["epoch"]


def test_shared_budget_is_atomic(store):
    def reserve(_):
        other = Store(store.database_url)
        try:
            return other.reserve_budget("account", "turns", 1, 3)["allowed"]
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(reserve, range(8))) == 3
    assert store.get_budget("account", "turns")["used"] == 3


def test_every_model_mutation_rejects_expired_lease(store, monkeypatch):
    job = store.create_job({"goal": "x"})
    store.create_task(job["id"], "plan", {}, "parent")
    task = store.claim_task("worker", lease_seconds=1)
    lease = {"task_id": task["id"], "worker_id": "worker", "fence": task["fence"], "revision": 1}
    monkeypatch.setattr(store_module, "utcnow", lambda: datetime.now(timezone.utc) + timedelta(seconds=2))
    mutations = [
        lambda: store.upsert_resource(job["id"], {"id": "late"}, lease=lease),
        lambda: store.add_artifact(job["id"], {"id": "late"}, lease=lease),
        lambda: store.record_observation(job["id"], {"kind": "late"}, lease=lease),
        lambda: store.create_task(job["id"], "fetch", {}, "late", lease=lease),
        lambda: store.commit_discovery(job["id"], "archive", "next", [{"id": "late"}], [], lease=lease),
    ]
    for mutation in mutations:
        with pytest.raises(LeaseLost):
            mutation()
    assert store.resources(job["id"]) == []
    assert store.artifacts(job["id"]) == []
    assert store.observations(job["id"]) == []
    assert store.get_checkpoint(job["id"], "archive") is None


def test_rate_slots_survive_restart_and_penalties_delay_waiters(store, monkeypatch):
    clock = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    monkeypatch.setattr(store_module, "utcnow", lambda: clock[0])
    one = store.acquire_rate_slot('profile:publisher', 2)
    two = store.acquire_rate_slot('profile:publisher', 2)
    assert one['delay_seconds'] == 0
    assert two['delay_seconds'] == 2
    assert store.confirm_rate_slot('profile:publisher', one['slot_at'])['allowed']
    other = Store(store.database_url)
    try:
        third = other.acquire_rate_slot('profile:publisher', 1)
        assert third['delay_seconds'] == 4
        other.penalize_rate('profile:publisher', 10)
        clock[0] += timedelta(seconds=2)
        assert store.confirm_rate_slot('profile:publisher', two['slot_at'])['delay_seconds'] == 8
        clock[0] += timedelta(seconds=8)
        assert store.confirm_rate_slot('profile:publisher', two['slot_at'])['allowed']
        # All queued permits cannot burst together at Retry-After expiration.
        assert other.confirm_rate_slot('profile:publisher', third['slot_at'])['delay_seconds'] == 2
        clock[0] += timedelta(seconds=2)
        assert other.confirm_rate_slot('profile:publisher', third['slot_at'])['allowed']
    finally:
        other.close()


def test_shared_job_concurrency_cap_and_expired_reclaim(store, monkeypatch):
    job = store.create_job({'goal': 'parallel', 'budget': {'max_agent_workers': 2}})
    for number in range(8):
        store.create_task(job['id'], 'retrieve', {'number': number}, str(number))
    stores = [Store(store.database_url) for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(lambda pair: pair[1].claim_task('worker-' + str(pair[0]), 10), enumerate(stores)))
        active = [task for task in claims if task]
        assert len(active) == 2
        assert store.claim_task('extra') is None
        future = datetime.now(timezone.utc) + timedelta(seconds=11)
        monkeypatch.setattr(store_module, 'utcnow', lambda: future)
        replacement = store.claim_task('replacement')
        assert replacement['id'] == active[0]['id'] or replacement['id'] == active[1]['id']
        assert replacement['fence'] == 2
        assert store.claim_task('replacement-2')
        assert store.claim_task('extra') is None
    finally:
        for item in stores:
            item.close()


def test_atomic_heartbeat_pause_guard_and_worker_catalog_persistence(store):
    job = store.create_job({'goal': 'x'})
    store.create_task(job['id'], 'plan', {}, 'root')
    task = store.claim_task('host')
    assert store.validate_task_lease(task['id'], 'host', task['fence'])['id'] == task['id']
    store.update_job(job['id'], status='paused')
    with pytest.raises(LeaseLost, match='paused'):
        store.heartbeat(task['id'], 'host', task['fence'], allowed_job_states=('running', 'queued'))
    assert store.get_task(task['id'])['lease_expires_at'] == task['lease_expires_at']
    store.register_worker({'id': 'host', 'models': [{'model': 'private-catalog'}]})
    store.register_worker({'id': 'host', 'state': 'idle'})
    other = Store(store.database_url)
    try:
        assert other.get_worker('host')['models'] == [{'model': 'private-catalog'}]
        assert other.list_workers()[0]['state'] == 'idle'
    finally:
        other.close()


def test_job_metadata_commit_rejects_cross_job_and_expired_lease(store, monkeypatch):
    job = store.create_job({'goal': 'first'})
    other = store.create_job({'goal': 'second'})
    store.create_task(job['id'], 'plan', {}, 'root')
    task = store.claim_task('host', lease_seconds=5)
    lease = {'task_id': task['id'], 'worker_id': 'host', 'fence': task['fence'], 'revision': task['revision']}
    with pytest.raises(LeaseLost, match='another job'):
        store.update_job(other['id'], lease=lease, inventory={'enumeration_complete': True})
    future = datetime.now(timezone.utc) + timedelta(seconds=6)
    monkeypatch.setattr(store_module, 'utcnow', lambda: future)
    with pytest.raises(LeaseLost):
        store.update_job(job['id'], lease=lease, inventory={'enumeration_complete': True})
    assert 'inventory' not in store.get_job(job['id'])
    assert 'inventory' not in store.get_job(other['id'])
