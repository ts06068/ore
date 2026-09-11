"""Synthetic-only evaluation fixtures: no real model call or empirical report."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ore.engine as engine_module
from ore.config import Settings
from ore.engine import Engine
from ore.evaluation import (BASELINE_EFFORT, BASELINE_MODEL, SCHEMA_VERSION, EvaluationError,
                            evaluate_job, load_evaluation_report, run_paired_evaluation, validate_routing_profile, runtime_fingerprint)
from ore.models import Mission, canonical_digest
from test_engine import ScriptedBackend, decision, engine


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def semantic(mission):
    value = Mission.model_validate(mission).model_dump(mode="json", by_alias=True, exclude_none=True)
    return {k: v for k, v in value.items() if k not in {"routing", "model_policy", "model", "effort"}}


def case(index):
    ident = f"fixture-{index}"
    return {"fixture_id": ident, "fixture_sha256": digest("synthetic source " + ident), "kind": "classify",
            "mission": {"goal": ident, "artifact_roles": [], "budget": {"max_turns": 5, "max_seconds": 5, "max_agent_workers": 1}},
            "rune": {"protocol_id": "fixture.classifier", "protocol_version": "1", "instructions": "Classify the explicitly declared fixture only"},
            "expectation": {"included_resource_keys": [ident], "excluded_resource_keys": [], "artifacts": []},
            "source_state": {"mode": "fixture_replay"}}


class EvaluationBackend(ScriptedBackend):
    async def models(self):
        return [{"model": name, "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in ("low", "medium", "high", "xhigh")]}
                for name in ("gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-sol")]

    async def run(self, *args, **kwargs):
        result = await super().run(*args, **kwargs)
        result["usage"] = {"inputTokens": 20, "outputTokens": 5, "totalTokens": 25}
        return result


async def test_paired_runner_uses_same_contract_separate_state_and_synthetic_label(tmp_path, monkeypatch):
    instances = []
    monkeypatch.setattr(engine_module, "BrowserManager", lambda *a, **kw: SimpleNamespace(close=AsyncMock()))
    def backend(*args, **kwargs):
        return EvaluationBackend(lambda task: [decision("resource", id=task["input"]["goal"], title="Fixture resource", classification="included"), decision("finish", summary="Fixture classified")])
    monkeypatch.setattr(engine_module, "CodexBackend", backend)
    def factory(settings):
        value = Engine(settings)
        instances.append(value)
        return value
    settings = Settings(state_dir=tmp_path, max_workers=1, auth_token="unused-synthetic-operator")
    report = await run_paired_evaluation(settings, [case(1), case(2)], engine_factory=factory, poll_interval=0.01, timeout_seconds=3)
    assert report["synthetic"] is True and report["evidence_kind"] == "synthetic_fixture"
    assert report["quality_pass"] is True and report["paired_outputs_equal"] is True
    assert report["architecture_superiority_tested"] is False
    assert report["cost_savings_proven"] is False and report["monetary_cost"] is None
    assert report["cases"][0]["order"] == ["baseline", "routed"]
    assert report["cases"][1]["order"] == ["routed", "baseline"]
    assert len(instances) == 4
    for entry in report["cases"]:
        assert entry["baseline"]["semantic_input_digest"] == entry["routed"]["semantic_input_digest"] == entry["semantic_input_digest"]
        assert entry["baseline"]["protocol_digest"] == entry["routed"]["protocol_digest"]
        assert entry["baseline"]["state_dir"] != entry["routed"]["state_dir"]
        assert entry["baseline"]["usage"]["totals"]["total_tokens"] == 50
        assert entry["routed"]["observed_routes"][0]["model"] == BASELINE_MODEL
    loaded = load_evaluation_report(tmp_path, report["report_ref"], report["report_sha256"])
    assert loaded["report"]["evaluation_id"] == report["evaluation_id"]


def test_quality_metrics_rehash_files_and_compare_independent_expectation(engine, tmp_path):
    job = engine.create({"goal": "Fixture excerpt", "artifact_roles": ["attachment"]})
    resource = engine.store.upsert_resource(job["id"], {"id": "r1", "title": "Fixture", "classification": "included"})
    path = engine.vault.root / "original.txt"
    path.write_text("the exact expected source text")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact = engine.store.add_artifact(job["id"], {"resource_id": resource["id"], "role": "attachment", "sha256": sha,
        "path": str(path), "version": "fixture-v1", "status": "verified", "integrity": "verified", "identity": "not_required", "bytes": path.stat().st_size})
    task = engine.store.claim_task("fixture", job_id=job["id"])
    engine.store.finish_task(task["id"], "fixture", task["fence"], {}, task["revision"])
    engine.store.update_job(job["id"], status="completed")
    expected = {"included_resource_keys": ["r1"], "excluded_resource_keys": [], "artifacts": [{"resource_key": "r1", "role": "attachment", "sha256": sha, "version": "fixture-v1"}]}
    first = evaluate_job(engine, job["id"], expected)
    assert first["quality_pass"] is True
    assert first["usage"]["totals"]["total_tokens"] is None
    path.write_text("a corrupted replacement")
    changed = evaluate_job(engine, job["id"], expected)
    assert changed["quality_pass"] is False
    assert artifact["id"] in changed["artifact_hash_failures"]
    wrong = evaluate_job(engine, job["id"], {**expected, "included_resource_keys": ["missing-id"]})
    assert wrong["missing_resource_keys"] == ["missing-id"]
    assert wrong["unexpected_resource_keys"] == ["r1"]


def report_envelope_for_validator_tests():
    """Synthetic parser fixture for the accepted envelope; never operational evidence.

The empirical discriminator is a field under test. These files only exist in
pytest temporary directories and are not produced by an empirical evaluation.
"""
    cases = []
    for index in range(5):
        source = case(index)
        source["mission"] = semantic(source["mission"])
        expectation = source["expectation"]
        manifest = deepcopy(expectation)
        arm = {"job_id": f"fixture-job-{index}", "job_status": "completed", "quality_pass": True, "quality_failures": [],
               "protocol_digest": canonical_digest(source["rune"]), "semantic_input_digest": canonical_digest(source["mission"]),
               "audit": {"status": "complete_within_scope", "gaps": []}, "failure_count": 0,
               "manifest": manifest, "output_manifest_digest": canonical_digest(manifest), "artifacts": [],
               "usage": {"decision_count": 2}, "observed_routes": [{"model": BASELINE_MODEL, "effort": BASELINE_EFFORT, "kind": "classify", "count": 2}]}
        routed = deepcopy(arm)
        routed["observed_routes"] = [{"model": "gpt-5.6-terra", "effort": "low", "kind": "classify", "count": 2}]
        cases.append({**source, "semantic_input_digest": canonical_digest(source["mission"]), "expectation_digest": canonical_digest(expectation),
                      "baseline": arm, "routed": routed})
    return {"schema_version": SCHEMA_VERSION, "comparison": "fixed_astra_vs_auto_same_runtime",
            "evidence_kind": "empirical", "synthetic": False, "protocol_digest": cases[0]["baseline"]["protocol_digest"],
            "kind": "classify", "fixture_ids": [item["fixture_id"] for item in cases], "cases": cases,
            "runtime_changed_during_evaluation": False, "runtime": runtime_fingerprint()}


def store_test_envelope(tmp_path, report):
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    path = reports / "synthetic-validator-fixture.json"
    raw = json.dumps(report).encode()
    path.write_bytes(raw)
    profile = {"report_ref": path.name, "report_sha256": hashlib.sha256(raw).hexdigest(),
               "protocol_digest": report["protocol_digest"], "kind": "classify", "fixture_ids": report["fixture_ids"],
               "model": "gpt-5.6-terra", "effort": "low", "passed": True}
    return path, profile


def check(tmp_path, profile, **overrides):
    return validate_routing_profile(profile, state_dir=tmp_path, protocol_digest=profile["protocol_digest"], kind="classify", **overrides)


def test_validator_binds_sha_protocol_kind_and_all_fixture_ids(tmp_path):
    report = report_envelope_for_validator_tests()
    path, profile = store_test_envelope(tmp_path, report)
    accepted = check(tmp_path, profile, fixture_ids=profile["fixture_ids"])
    assert accepted["valid"] is True
    assert accepted["profile"]["empirical_paired_cases"] == 5
    assert accepted["profile"]["cost_savings_proven"] is False
    assert check(tmp_path, {**profile, "report_sha256": ""})["valid"] is False
    assert check(tmp_path, {**profile, "kind": "retrieve"})["valid"] is False
    assert check(tmp_path, profile, fixture_ids=["different"])["valid"] is False
    assert validate_routing_profile(profile, state_dir=tmp_path, protocol_digest="different", kind="classify")["valid"] is False
    path.write_text(path.read_text() + " ")
    assert "SHA-256 mismatch" in " ".join(check(tmp_path, profile)["reasons"])


@pytest.mark.parametrize("change,expected_reason", [
    (lambda r: r.update(synthetic=True, evidence_kind="synthetic_fixture"), "empirical_evidence_required"),
    (lambda r: r["cases"].pop(), "at_least_five_distinct_paired_cases_required"),
    (lambda r: r["cases"][0]["routed"].update(quality_pass=False), "both_arms_must_pass_quality"),
    (lambda r: r["cases"][0]["routed"].update(failure_count=1), "routed_failure_count_exceeds_baseline"),
    (lambda r: r["cases"][0]["routed"].update(observed_routes=[{"model": BASELINE_MODEL, "effort": "high", "kind": "classify", "count": 2}]), "claimed_downgrade_was_not_observed"),
    (lambda r: r["cases"][0]["routed"].update(semantic_input_digest="changed"), "paired_inputs_differ"),
    (lambda r: r["cases"][0]["routed"]["manifest"].update(included_resource_keys=["wrong"]), "output_manifest_digest_mismatch"),
    (lambda r: r.update(runtime_changed_during_evaluation=True), "runtime_equivalence_not_established"),
    (lambda r: r["runtime"].update(digest="previous-build"), "evaluation_runtime_differs_from_current_runtime"),
    (lambda r: r.pop("runtime"), "evaluation_runtime_differs_from_current_runtime"),
])
def test_validator_rejects_unproven_or_degraded_pairs(tmp_path, change, expected_reason):
    report = report_envelope_for_validator_tests()
    change(report)
    _, profile = store_test_envelope(tmp_path, report)
    result = check(tmp_path, profile)
    assert result["valid"] is False
    assert expected_reason in result["reasons"]


def test_report_loader_rejects_outside_path_symlink_and_malformed_envelope(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{}')
    with pytest.raises(EvaluationError):
        load_evaluation_report(tmp_path, "../outside.json")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "link.json").symlink_to(outside)
    with pytest.raises(EvaluationError):
        load_evaluation_report(tmp_path, "link.json")
    report = report_envelope_for_validator_tests()
    report["cases"][0]["routed"]["observed_routes"] = [None]
    _, profile = store_test_envelope(tmp_path, report)
    assert check(tmp_path, profile)["valid"] is False


async def test_runner_rejects_duplicate_or_unbound_reference_before_engine_creation(tmp_path):
    called = False
    def factory(settings):
        nonlocal called
        called = True
        raise AssertionError("No engine should be created")
    with pytest.raises(EvaluationError):
        await run_paired_evaluation(Settings(state_dir=tmp_path), [case(1), case(1)], engine_factory=factory)
    bad = case(1)
    bad.pop("expectation")
    with pytest.raises(EvaluationError):
        await run_paired_evaluation(Settings(state_dir=tmp_path), [bad], engine_factory=factory)
    assert called is False


async def test_runner_timeout_stops_actor_and_records_failed_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "BrowserManager", lambda *a, **kw: SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(engine_module, "CodexBackend", lambda *a, **kw: EvaluationBackend(block=True))
    instances = []
    def factory(settings):
        value = Engine(settings)
        instances.append(value)
        return value
    result = await run_paired_evaluation(Settings(state_dir=tmp_path, max_workers=1), [case(1)],
        engine_factory=factory, timeout_seconds=0.1, poll_interval=0.01)
    assert result["quality_pass"] is False
    assert all(result["cases"][0][arm]["timeout"] for arm in ("baseline", "routed"))
    assert all(result["cases"][0][arm]["job_status"] == "paused" for arm in ("baseline", "routed"))
    assert all(instance.backend.interrupted for instance in instances)


async def test_explicit_candidate_bootstrap_is_fixed_experimental_and_remains_synthetic(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, "BrowserManager", lambda *a, **kw: SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(engine_module, "CodexBackend", lambda *a, **kw: EvaluationBackend(lambda task: [
        decision("resource", id=task["input"]["goal"], title="Reference", classification="included"),
        decision("finish", summary="Reference classified")]))
    source = case(7)
    source["mission"]["access_profile_ref"] = "paired-local"
    progress = []
    report = await run_paired_evaluation(Settings(state_dir=tmp_path, max_workers=1), [source],
        engine_factory=lambda settings: Engine(settings), timeout_seconds=3, poll_interval=0.01,
        candidate_profile={"model": "gpt-5.6-terra", "effort": "low"},
        access_profile={"id": "paired-local", "allowed_hosts": ["127.0.0.1"]}, on_progress=progress.append)
    assert report["quality_pass"] is True
    assert report["comparison"] == "fixed_astra_vs_experimental_profile_same_runtime"
    assert report["routed_policy"] == {"mode": "experimental_profile", "model": "gpt-5.6-terra", "effort": "low"}
    assert report["synthetic"] is True
    entry = report["cases"][0]
    assert entry["routed"]["observed_routes"] == [{"model": "gpt-5.6-terra", "effort": "low", "kind": "classify", "count": 2}]
    assert entry["baseline"]["semantic_input_digest"] == entry["routed"]["semantic_input_digest"] == entry["semantic_input_digest"]
    assert entry["mission"]["access_profile_ref"] == "paired-local"
    assert len(progress) == 4
    for arm in ("baseline", "routed"):
        profiles = json.loads((Path(entry[arm]["state_dir"]) / "access-profiles.json").read_text())
        assert not any("routing_validation" in profile for profile in profiles)


def test_validator_accepts_bound_experimental_candidate_envelope_and_rejects_mislabeling(tmp_path):
    report = report_envelope_for_validator_tests()
    report["comparison"] = "fixed_astra_vs_experimental_profile_same_runtime"
    report["routed_policy"] = {"mode": "experimental_profile", "model": "gpt-5.6-terra", "effort": "low"}
    _, profile = store_test_envelope(tmp_path, report)
    assert check(tmp_path, profile)["valid"] is True
    report["routed_policy"]["mode"] = "quality_constrained_auto"
    _, profile = store_test_envelope(tmp_path, report)
    assert "experimental_profile_does_not_match_target" in check(tmp_path, profile)["reasons"]


def test_validator_requires_rehashed_evidence_for_every_manifest_artifact(tmp_path):
    report = report_envelope_for_validator_tests()
    item = report["cases"][0]
    expected = {"resource_key": item["expectation"]["included_resource_keys"][0], "role": "excerpt", "sha256": digest("fixture bytes")}
    item["expectation"]["artifacts"] = [expected]
    item["expectation_digest"] = canonical_digest(item["expectation"])
    for arm in ("baseline", "routed"):
        item[arm]["manifest"]["artifacts"] = [expected]
        item[arm]["output_manifest_digest"] = canonical_digest(item[arm]["manifest"])
        item[arm]["artifacts"] = []  # A claimed manifest hash without original-byte measurement is insufficient.
    _, profile = store_test_envelope(tmp_path, report)
    assert "artifact_measurements_do_not_match_manifest" in check(tmp_path, profile)["reasons"]


def test_runtime_fingerprint_includes_companion_execution_assets():
    files=runtime_fingerprint()["files"]
    assert {"ore/companion_extension/background.js", "ore/companion_extension/protocol.js", "ore/companion_extension/manifest.json"}.issubset(files)
