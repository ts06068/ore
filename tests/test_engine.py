"""Coordinator contracts using scripted decisions, never an external model/browser."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ore.engine as engine_module
from ore.config import Settings
from ore.engine import Engine
from ore.policy import AccessDenied
from ore.providers import DECISION_SCHEMA
from ore.store import LeaseLost
from ore.tools import ToolRuntime


CATALOG = [{"id": "fixture-model", "model": "fixture-model",
            "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "xhigh"}]}]


def mission(**extra):
    return {"goal": "Collect the explicitly bounded fixture collection", "model_policy": "fixed",
            "model": "fixture-model", "effort": "high", "artifact_roles": [],
            "budget": {"max_turns": 20, "max_seconds": 10, "max_agent_workers": 2}, **extra}


def decision(tool, **arguments):
    return {"tool": tool, "arguments": json.dumps(arguments), "reason": "Exercise a bounded fixture action"}


class ScriptedBackend:
    """Each thread has its own action sequence and optional overlap barrier."""
    binary = "/fixture/not-executed-codex"

    def __init__(self, script=None, block=False, overlap=1):
        self.script = script or (lambda task: [decision("finish", summary="Fixture completed")])
        self.threads = {}
        self.calls = []
        self.interrupted = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block
        self.overlap = overlap
        self.active = 0
        self.max_active = 0

    async def models(self):
        return CATALOG

    async def thread(self, tools, handler, **options):
        ident = f"fixture-thread-{len(self.threads) + 1}"
        task = handler.__self__.task
        self.threads[ident] = {"decisions": list(self.script(task)), "index": 0, "task": task, "options": options, "tools": tools}
        return ident

    async def run(self, thread_id, prompt, **options):
        state = self.threads[thread_id]
        self.calls.append({"thread_id": thread_id, "prompt": prompt, **options})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= self.overlap:
            self.entered.set()
        try:
            if self.block:
                await self.release.wait()
            value = state["decisions"][state["index"]]
            state["index"] += 1
            return {"text": json.dumps(value), "turn": {"status": "completed", "usage": {"fixture_turns": 1}}}
        finally:
            self.active -= 1

    async def interrupt(self, thread_id):
        self.interrupted.append(thread_id)

    async def close(self):
        return None


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    monkeypatch.delenv("ORE_SECRET_KEY", raising=False)
    monkeypatch.setattr(engine_module, "BrowserManager", lambda *a, **kw: SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(engine_module, "CodexBackend", lambda *a, **kw: ScriptedBackend())
    value = Engine(Settings(state_dir=tmp_path / "state", auth_token="synthetic-test-operator", max_workers=2))
    yield value
    await value.stop()


async def eventually(predicate, timeout=4):
    async def check():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(check(), timeout)


async def test_actor_closed_schema_recovers_invalid_action_and_records_evidence(engine):
    engine.backend = ScriptedBackend(lambda task: [
        decision("resource", id="fixture-a", title="Study", classification="included", shell="must be rejected"),
        decision("resource", id="fixture-a", title="Study", classification="included", reason="Within bounded collection"),
        decision("finish", summary="One source item assessed and included"),
    ])
    job = engine.create(mission())
    task = engine.store.claim_task("fixture-worker", job_id=job["id"])
    await engine.execute_task(task, "fixture-worker")
    engine.reconcile(job["id"])
    assert engine.store.get_task(task["id"])["state"] == "succeeded"
    assert engine.store.get_job(job["id"])["status"] == "completed"
    assert engine.store.resources(job["id"])[0]["classification"] == "included"
    assert "shell" not in engine.store.resources(job["id"])[0]
    assert all(call["output_schema"] == DECISION_SCHEMA for call in engine.backend.calls)
    assert engine.backend.threads["fixture-thread-1"]["tools"] == []
    events = engine.store.events(job["id"])
    assert any(event["type"] == "tool_failed" for event in events)
    assert sum(event["type"] == "agent_decision" for event in events) == 3
    assert "tool_error" in engine.backend.calls[1]["prompt"]


async def test_actor_finish_cannot_override_independent_audit(engine):
    engine.backend = ScriptedBackend(lambda task: [decision("finish", summary="Everything is complete")])
    job = engine.create(mission(artifact_roles=["main_pdf", "supplement"]))
    task = engine.store.claim_task("fixture-worker", job_id=job["id"])
    await engine.execute_task(task, "fixture-worker")
    engine.reconcile(job["id"])
    finished = engine.store.get_job(job["id"])
    assert finished["status"] == "needs_review"
    assert finished["audit"]["status"] == "incomplete"
    assert {gap["kind"] for gap in finished["audit"]["gaps"]} == {"no_discovery_evidence"}


async def test_parallel_agent_threads_overlap_and_preserve_independent_tasks(engine):
    engine.backend = ScriptedBackend(lambda task: [
        decision("resource", id=task["id"], title=task["input"]["goal"], classification="included"),
        decision("finish", summary="Assessed bounded child collection"),
    ], block=True, overlap=2)
    job = engine.create(mission())
    engine.store.create_task(job["id"], "retrieve", {"goal": "Second bounded collection", "urls": []}, "second-collection")
    await engine.run(job["id"])
    await asyncio.wait_for(engine.backend.entered.wait(), 4)
    assert engine.backend.max_active == 2
    assert len(engine.running) == 2
    engine.backend.release.set()
    await eventually(lambda: engine.store.get_job(job["id"])["status"] == "completed")
    assert len(engine.backend.threads) == 2
    assert len(engine.store.resources(job["id"])) == 2
    assert all(task["state"] == "succeeded" for task in engine.store.tasks(job["id"]))


async def test_pause_interrupts_inflight_model_and_resume_uses_new_fence(engine):
    engine.backend = ScriptedBackend(block=True)
    job = engine.create(mission())
    await engine.run(job["id"])
    await asyncio.wait_for(engine.backend.entered.wait(), 4)
    task = engine.store.tasks(job["id"])[0]
    old_fence = task["fence"]
    await engine.pause(job["id"])
    assert engine.store.get_job(job["id"])["status"] == "paused"
    assert engine.store.get_task(task["id"])["state"] == "paused"
    assert engine.backend.interrupted == ["fixture-thread-1"]
    with pytest.raises(LeaseLost):
        engine.store.finish_task(task["id"], task["worker_id"], old_fence, {}, task["revision"])
    engine.backend.release.set()
    await engine.run(job["id"])
    await eventually(lambda: engine.store.get_task(task["id"])["state"] == "succeeded")
    assert engine.store.get_task(task["id"])["fence"] > old_fence
    assert len(engine.backend.threads) == 2


async def test_revision_and_generation_reject_old_runtime_side_effects(engine):
    engine.start = AsyncMock()
    job = engine.create(mission())
    task = engine.store.claim_task("old-worker", job_id=job["id"])
    runtime = ToolRuntime(engine, job["id"], task)
    revised = await engine.revise(job["id"], mission(goal="A newly narrowed mission"))
    assert revised["revision"] == 2
    assert engine.store.get_task(task["id"])["state"] == "superseded"
    with pytest.raises((AccessDenied, LeaseLost)):
        await runtime.execute("resource", {"id": "stale", "title": "Stale write", "classification": "included"})
    assert engine.store.resources(job["id"]) == []
    replacement = engine.store.claim_task("new-worker", job_id=job["id"])
    newer_runtime = ToolRuntime(engine, job["id"], replacement)
    refreshed = await engine.refresh(job["id"])
    assert refreshed["generation"] == 2
    with pytest.raises((AccessDenied, LeaseLost)):
        await newer_runtime.execute("resource", {"id": "old-generation", "title": "Stale generation"})
    assert engine.store.resources(job["id"]) == []


async def test_runtime_rejects_stale_worker_fence_before_resource_write(engine):
    job = engine.create(mission())
    task = engine.store.claim_task("old-worker", job_id=job["id"])
    runtime = ToolRuntime(engine, job["id"], task)
    engine.store.fail_task(task["id"], "old-worker", task["fence"], {"code": "transient"}, task["revision"], retry_seconds=0)
    replacement = engine.store.claim_task("new-worker", job_id=job["id"])
    assert replacement["fence"] > task["fence"]
    with pytest.raises(LeaseLost):
        await runtime.execute("resource", {"id": "stale", "title": "Do not write"})
    assert engine.store.resources(job["id"]) == []


def test_audit_requires_supplement_inventory_and_every_expected_file(engine):
    job = engine.create(mission(artifact_roles=["main_pdf", "supplement"]))
    resource = engine.store.upsert_resource(job["id"], {"id": "r1", "title": "Trial", "classification": "included", "supplement_status": "present", "expected_supplements": 2})
    engine.store.add_artifact(job["id"], {"id": "main", "resource_id": resource["id"], "role": "main_pdf", "status": "verified", "integrity": "verified", "sha256": "a" * 64})
    engine.store.add_artifact(job["id"], {"id": "supp1", "resource_id": resource["id"], "role": "supplement", "status": "verified", "integrity": "verified", "sha256": "b" * 64})
    audit = engine.audit(job["id"])
    assert audit["status"] == "incomplete"
    assert any(gap["kind"] == "supplement_files_missing" and gap["verified"] == 1 for gap in audit["gaps"])
    engine.store.add_artifact(job["id"], {"id": "supp2", "resource_id": resource["id"], "role": "supplement", "status": "verified", "integrity": "verified", "sha256": "c" * 64})
    assert engine.audit(job["id"])["status"] == "complete_within_scope"


def test_audit_inventory_gap_cannot_be_overridden_by_complete_boolean(engine):
    job = engine.create(mission(completeness="inventory"))
    engine.store.upsert_resource(job["id"], {"id": "r1", "title": "In-scope resource", "classification": "included"})
    engine.store.update_job(job["id"], inventory={"enumeration_complete": True, "expected_resources": 1,
        "official_toc_evidence": ["https://publisher.example/issue/1"], "gaps": ["Issue 2 could not be inspected"]})
    audit = engine.audit(job["id"])
    assert audit["status"] == "incomplete", "Known enumeration gaps must survive the final audit"
    assert any(gap["kind"] == "inventory_gaps" for gap in audit["gaps"])


def test_all_excluded_collection_requires_review(engine):
    job = engine.create(mission())
    engine.store.upsert_resource(job["id"], {"id": "excluded", "title": "Outside collection", "classification": "excluded"})
    result = engine.audit(job["id"])
    assert result["status"] == "incomplete"
    assert any(gap["kind"] == "no_included_resources_review_required" for gap in result["gaps"])


async def test_inventory_requires_observed_evidence_and_delegate_is_idempotent(engine):
    job = engine.create(mission(completeness="inventory"))
    runtime = ToolRuntime(engine, job["id"])
    args = {"expected_resources": 0, "enumeration_complete": True, "official_toc_evidence": ["https://publisher.example/issue/1"]}
    with pytest.raises(AccessDenied):
        await runtime.execute("inventory", args)
    engine.store.record_observation(job["id"], {"kind": "page", "url": args["official_toc_evidence"][0]})
    assert (await runtime.execute("inventory", args))["enumeration_complete"] is True
    delegated = {"goal": "Collect one issue", "key": "issue-1", "kind": "retrieve", "urls": []}
    first = await runtime.execute("delegate", delegated)
    second = await runtime.execute("delegate", delegated)
    assert first["id"] == second["id"]
    with pytest.raises(ValueError):
        await runtime.execute("delegate", {**delegated, "goal": "Different scope under reused key"})
    assert len(engine.store.tasks(job["id"])) == 2
