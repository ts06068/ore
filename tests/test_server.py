"""Authenticated ASGI tests. No external browser, model, account or network calls."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from ore.server import create_app
from test_engine import CATALOG, engine, mission


@pytest.fixture
async def client(engine):
    # Control routes are real, but starting unrelated background model work is not
    # needed to verify authenticated API/lease semantics.
    engine.start = AsyncMock()
    app = create_app(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://ore.test") as value:
        yield value


async def login(client):
    response = await client.post("/v1/auth/login", json={"token": "synthetic-test-operator"})
    assert response.status_code == 200
    return response


async def create_job(client, **overrides):
    response = await client.post("/v1/jobs", json={"mission": mission(**overrides)})
    assert response.status_code == 201, response.text
    job=response.json()
    assert job['status']=='draft'
    assert (await client.post(f"/v1/jobs/{job['id']}/run")).status_code==200
    return job


async def test_authentication_cookie_logout_and_no_secret_echo(client):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/v1/jobs")).status_code == 401
    denied = await client.post("/v1/auth/login", json={"token": "invalid-synthetic-token"})
    assert denied.status_code == 401
    response = await login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert response.json() == {"authenticated": True}
    status = await client.get("/v1/auth/status")
    assert status.status_code == 200
    assert "synthetic-test-operator" not in status.text
    assert status.headers["cache-control"] == "no-store"
    assert status.headers["x-content-type-options"] == "nosniff"
    assert (await client.post("/v1/auth/logout")).status_code == 200
    assert (await client.get("/v1/jobs")).status_code == 401
    bearer = await client.get("/v1/jobs", headers={"Authorization": "Bearer synthetic-test-operator"})
    assert bearer.status_code == 200


async def test_cross_origin_mutations_rejected_before_login_and_with_cookie(client, engine):
    bad_origin = {"Origin": "https://attacker.invalid"}
    assert (await client.post("/v1/auth/login", json={"token": "synthetic-test-operator"}, headers=bad_origin)).status_code == 403
    await login(client)
    assert (await client.post("/v1/jobs", json=mission(), headers=bad_origin)).status_code == 403
    assert engine.store.list_jobs() == []
    allowed = await client.post("/v1/jobs", json=mission(), headers={"Origin": "https://ore.test"})
    assert allowed.status_code == 201


async def test_job_create_read_revise_pause_refresh_and_export(client, engine):
    await login(client)
    job = await create_job(client)
    ident = job["id"]
    assert (await client.get(f"/v1/jobs/{ident}")).json()["revision"] == 1
    assert len((await client.get("/v1/jobs")).json()) == 1
    updated = await client.patch(f"/v1/jobs/{ident}", json={"mission": {"goal": "A narrowed fixture goal"}})
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["mission"]["goal"] == "A narrowed fixture goal"
    assert (await client.post(f"/v1/jobs/{ident}/pause")).json()["status"] == "paused"
    refreshed = await client.post(f"/v1/jobs/{ident}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["generation"] == 2
    resource = engine.store.upsert_resource(ident, {"id": "fixture-resource", "title": "Excluded item", "classification": "excluded"})
    exported = await client.get(f"/v1/jobs/{ident}/export?format=jsonl")
    assert exported.status_code == 200
    rows = [json.loads(line) for line in exported.text.splitlines()]
    assert {r["type"] for r in rows} >= {"job", "audit", "resource"}
    assert any(r["type"] == "resource" and r["data"]["id"] == resource["id"] for r in rows)
    assert (await client.get("/v1/jobs/missing")).status_code == 404
    assert (await client.get(f"/v1/jobs/{ident}/export?format=unsupported")).status_code == 422


async def test_rune_validation_update_read_and_source_capabilities(client):
    await login(client)
    payload = {"protocol_id": "fixture", "protocol_version": "0.1.0", "instructions": "Collect the specified fixture", "scope": {"origins": ["https://example.org"]}}
    validated = await client.post("/v1/runes/validate", json=payload)
    assert validated.status_code == 200
    assert validated.json()["valid"] is True
    first_digest = validated.json()["digest"]
    saved = await client.put("/v1/runes/fixture", json=payload)
    assert saved.status_code == 200
    fetched = await client.get("/v1/runes/fixture")
    assert fetched.status_code == 200 and fetched.json()["instructions"] == payload["instructions"]
    payload["instructions"] = "A changed but bounded fixture protocol"
    second = await client.post("/v1/runes/validate", json=payload)
    assert second.json()["digest"] != first_digest
    invalid = await client.post("/v1/runes/validate", json={"protocol_version": ""})
    assert invalid.status_code == 422
    sources = {entry["id"]: entry for entry in (await client.get("/v1/sources")).json()}
    assert sources["google_scholar"]["capabilities"]["search"] is False
    assert sources["pmc"]["capabilities"]["resolve"] is True


async def test_secret_store_only_returns_reference_and_encrypts_value(client, engine):
    await login(client)
    value = "fixture-only-value-not-for-network"
    stored = await client.put("/v1/secrets/research/scopus-test", json={"value": value})
    assert stored.status_code == 200
    assert stored.json() == {"ref": "research/scopus-test", "stored": True}
    assert value not in stored.text
    assert engine.secrets.get("research/scopus-test") == value
    assert value.encode() not in (engine.settings.state_dir / "secrets.enc").read_bytes()
    status = (await client.get("/v1/auth/status")).json()
    assert "research/scopus-test" in status["configured_secret_refs"]
    assert value not in json.dumps(status)
    profile = {"id": "institution", "sources": {"scopus": {"api_key_ref": "research/scopus-test"}}}
    saved = await client.post("/v1/access-profiles", json=profile)
    assert saved.status_code == 200
    assert saved.json()["sources"]["scopus"]["api_key_ref"] == "research/scopus-test"
    assert value not in (await client.get("/v1/access-profiles")).text


async def test_nested_literal_secret_in_profile_is_rejected(client, engine):
    await login(client)
    value = "fixture-literal-must-not-be-persisted"
    response = await client.post("/v1/access-profiles", json={"id": "bad-profile", "sources": {"wos": {"api_key": value}}})
    assert response.status_code in (403, 422), "Nested source config must use secret references"
    if engine.profile_path.exists():
        assert value not in engine.profile_path.read_text()


async def test_remote_worker_fence_identity_and_pause_are_enforced(client, engine):
    await login(client)
    job = await create_job(client)
    for worker in ("worker-one", "worker-two"):
        assert (await client.post("/v1/workers/register", json={"id": worker, "models": CATALOG})).status_code == 200
    claimed = await client.post("/v1/workers/worker-one/claim")
    assert claimed.status_code == 200
    task = claimed.json()["task"]
    assert task["job_id"] == job["id"]
    wrong = await client.post(f"/v1/workers/worker-two/tasks/{task['id']}/tool", json={"fence": task["fence"], "tool": "resource", "arguments": {"id": "forbidden", "title": "Wrong owner"}})
    assert wrong.status_code == 409
    assert engine.store.resources(job["id"]) == []
    stale = await client.post(f"/v1/workers/worker-one/tasks/{task['id']}/heartbeat", json={"fence": task["fence"] - 1})
    assert stale.status_code == 409
    action = await client.post(f"/v1/workers/worker-one/tasks/{task['id']}/tool", json={"fence": task["fence"], "tool": "resource", "arguments": {"id": "allowed", "title": "Bounded item", "classification": "excluded"}})
    assert action.status_code == 200
    assert len(engine.store.resources(job["id"])) == 1
    assert (await client.post(f"/v1/jobs/{job['id']}/pause")).status_code == 200
    paused = await client.post(f"/v1/workers/worker-one/tasks/{task['id']}/tool", json={"fence": task["fence"], "tool": "state", "arguments": {}})
    assert paused.status_code in (403, 409)


async def test_remote_worker_cannot_override_audit_or_turn_budget(client, engine):
    await login(client)
    job = await create_job(client, budget={"max_turns": 2, "max_seconds": 10, "max_agent_workers": 1})
    await client.post("/v1/workers/register", json={"id": "remote", "models": CATALOG})
    task = (await client.post("/v1/workers/remote/claim")).json()["task"]
    prefix = f"/v1/workers/remote/tasks/{task['id']}"
    for _ in range(2):
        response = await client.post(prefix + "/route", json={"fence": task["fence"]})
        assert response.status_code == 200
        assert response.json()["model"] == "fixture-model"
    assert (await client.post(prefix + "/route", json={"fence": task["fence"]})).status_code == 403
    # The remote worker's completion proposal cannot replace coordinator evidence.
    finished = await client.post(prefix + "/finish", json={"fence": task["fence"], "result": {"audit": {"status": "complete_within_scope"}, "summary": "claimed complete"}})
    assert finished.status_code == 200
    stored = engine.store.get_job(job["id"])
    assert stored["status"] == "needs_review"
    assert stored["audit"]["status"] == "incomplete"
    assert any(gap["kind"] == "no_discovery_evidence" for gap in stored["audit"]["gaps"])


async def test_remote_worker_cannot_invoke_unknown_or_open_argument_action(client):
    await login(client)
    await create_job(client)
    await client.post("/v1/workers/register", json={"id": "remote", "models": CATALOG})
    task = (await client.post("/v1/workers/remote/claim")).json()["task"]
    endpoint = f"/v1/workers/remote/tasks/{task['id']}/tool"
    denied = await client.post(endpoint, json={"fence": task["fence"], "tool": "shell", "arguments": {"command": "not executed"}})
    assert denied.status_code == 403
    invalid = await client.post(endpoint, json={"fence": task["fence"], "tool": "resource", "arguments": {"id": "bad", "title": "Closed schema", "unexpected": "rejected"}})
    assert invalid.status_code == 422


async def test_remote_old_revision_rejected_after_mission_edit(client, engine):
    await login(client)
    job = await create_job(client)
    await client.post("/v1/workers/register", json={"id": "remote", "models": CATALOG})
    task = (await client.post("/v1/workers/remote/claim")).json()["task"]
    updated = await client.patch(f"/v1/jobs/{job['id']}", json={"mission": {"goal": "Changed mission"}})
    assert updated.status_code == 200
    response = await client.post(f"/v1/workers/remote/tasks/{task['id']}/tool", json={"fence": task["fence"], "tool": "resource", "arguments": {"id": "stale", "title": "Old revision write"}})
    assert response.status_code == 409
    assert engine.store.resources(job["id"]) == []
