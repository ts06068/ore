"""Paired routing experiments and fail-closed, content-bound downgrade evidence.

This compares two routing policies inside the same ORE runtime. It does not run a
plain-agent architectural baseline, infer global recall, or estimate token prices.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import secrets
import time
from typing import Callable, TYPE_CHECKING

from .config import Settings
if TYPE_CHECKING:
    from .engine import Engine
from .models import Mission, Rune, canonical_digest

SCHEMA_VERSION = "ore.paired-evaluation/v1"
BASELINE_MODEL = "gpt-6-astra"
BASELINE_EFFORT = "high"
DOWNGRADE_TARGETS = {"retrieve": ("gpt-5.6-sol", "medium"), "extract": ("gpt-5.6-terra", "low"),
                     "classify": ("gpt-5.6-terra", "low")}
TERMINAL = {"completed", "needs_review", "blocked", "failed", "paused", "cancelled", "awaiting_user", "awaiting_auth", "awaiting_source", "paused_budget", "finished_incomplete"}
ROUTING_KEYS = {"model", "effort", "model_policy", "routing"}


class EvaluationError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_fingerprint() -> dict:
    root = Path(__file__).parent
    # Include all runtime modules and shipped extraction profiles. A calibration
    # from another build cannot authorize a cheaper route after an upgrade.
    files = {"ore/" + str(path.relative_to(root)): _hash_file(path)
             for path in sorted(root.rglob("*.py"))}
    for path in sorted((root / 'companion_extension').glob('*')):
        if path.is_file() and path.suffix in {'.js', '.json', '.html'}:
            files['ore/' + str(path.relative_to(root))] = _hash_file(path)
    desktop_assets = root / 'desktop_assets'
    if not desktop_assets.is_dir():
        desktop_assets = root.parents[1] / 'deploy' / 'desktop'
    if desktop_assets.is_dir():
        for path in sorted(desktop_assets.rglob('*')):
            if path.is_file() and (path.suffix in {'.py', '.json'} or path.name == 'Dockerfile'):
                files['ore/desktop_assets/' + str(path.relative_to(desktop_assets))] = _hash_file(path)
    from importlib.util import find_spec
    from importlib.metadata import PackageNotFoundError, version
    scholarly = find_spec("ore_scholarly")
    if scholarly and scholarly.origin:
        extension = Path(scholarly.origin).parent
        for path in sorted(extension.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json"}:
                files["ore_scholarly/" + str(path.relative_to(extension))] = _hash_file(path)
    dependencies = {}
    for name in ("pydantic", "httpx", "playwright", "pypdf", "beautifulsoup4", "defusedxml", "sqlalchemy", "jsonschema"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    value = {"python": platform.python_version(), "files": files, "dependencies": dependencies}
    return {**value, "digest": canonical_digest(value)}


def _semantic_mission(mission):
    value = Mission.model_validate(mission).model_dump(mode="json", by_alias=True, exclude_none=True)
    return {key: item for key, item in value.items() if key not in ROUTING_KEYS}


def _identity(resource):
    return str(resource.get("resource_key") or resource.get("canonical_id") or resource.get("doi") or resource.get("url") or resource["id"])


def _artifact_key(item):
    return (str(item.get("resource_key", "")), str(item.get("role", "")), str(item.get("sha256", "")), str(item.get("version") or ""))


def _usage(events):
    aliases = {"input_tokens": ("inputTokens", "input_tokens", "prompt_tokens"),
               "output_tokens": ("outputTokens", "output_tokens", "completion_tokens"),
               "total_tokens": ("totalTokens", "total_tokens")}
    decisions = [event for event in events if event["type"] == "agent_decision"]
    totals = {key: 0 for key in aliases}
    missing = {key: 0 for key in aliases}
    raw = []
    for event in decisions:
        usage = event.get("payload", {}).get("usage") or {}
        raw.append(usage)
        for key, names in aliases.items():
            found = next((usage[name] for name in names if isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool) and usage[name] >= 0), None)
            if found is None:
                missing[key] += 1
            else:
                totals[key] += found
    return {"decision_count": len(decisions), "totals": {key: totals[key] if decisions and missing[key] == 0 else None for key in totals},
            "observed_partial_totals": totals, "missing_by_field": missing, "raw_per_decision": raw,
            "monetary_cost": None, "price_schedule": None}


def evaluate_job(engine: Engine, job_id: str, expectation: dict | None = None) -> dict:
    """Recompute quality from audit, exact expected identities and original file bytes."""
    job = engine.store.get_job(job_id)
    if job is None:
        raise EvaluationError("Unknown evaluation job")
    resources, artifacts = engine.store.resources(job_id), engine.store.artifacts(job_id)
    tasks, events = engine.store.tasks(job_id), engine.store.events(job_id)
    audit = engine.audit(job_id)
    by_id = {item["id"]: _identity(item) for item in resources}
    included = sorted(_identity(item) for item in resources if (item.get("classification") or item.get("eligibility")) in ("included", "include", "original_article"))
    excluded = sorted(_identity(item) for item in resources if (item.get("classification") or item.get("eligibility")) in ("excluded", "exclude"))
    manifest, hash_failures = [], []
    for artifact in artifacts:
        entry = {"resource_key": by_id.get(artifact.get("resource_id"), "unbound"), "role": artifact.get("role"),
                 "sha256": artifact.get("sha256"), "version": artifact.get("version"), "status": artifact.get("status"),
                 "integrity": artifact.get("integrity"), "identity": artifact.get("identity"), "bytes": artifact.get("bytes")}
        path = Path(artifact.get("path") or "")
        try:
            valid_path = path.resolve().is_relative_to(engine.vault.root.resolve()) and path.is_file() and not path.is_symlink()
            actual = _hash_file(path) if valid_path else None
        except OSError:
            actual = None
        entry["recomputed_sha256"] = actual
        entry["hash_matches"] = bool(actual and actual == artifact.get("sha256"))
        if not entry["hash_matches"]:
            hash_failures.append(artifact["id"])
        manifest.append(entry)
    manifest.sort(key=lambda item: _artifact_key(item))
    problems = []
    if expectation is None:
        problems.append("independent_expectation_missing")
        expectation = {}
    expected_included = sorted(expectation.get("included_resource_keys", []))
    expected_excluded = sorted(expectation.get("excluded_resource_keys", []))
    missing = sorted((set(expected_included) - set(included)) | (set(expected_excluded) - set(excluded)))
    unexpected = sorted((set(included) - set(expected_included)) | (set(excluded) - set(expected_excluded)))
    if missing:
        problems.append("expected_resource_or_classification_missing")
    if unexpected:
        problems.append("unexpected_resource_or_classification")
    if "artifacts" not in expectation or "included_resource_keys" not in expectation or "excluded_resource_keys" not in expectation:
        problems.append("incomplete_expectation_contract")
    expected_artifacts = Counter(_artifact_key(item) for item in expectation.get("artifacts", []))
    actual_artifacts = Counter(_artifact_key(item) for item in manifest)
    missing_artifacts = list((expected_artifacts - actual_artifacts).elements())
    unexpected_artifacts = list((actual_artifacts - expected_artifacts).elements())
    if missing_artifacts or unexpected_artifacts:
        problems.append("artifact_identity_role_version_or_hash_mismatch")
    if hash_failures:
        problems.append("artifact_bytes_do_not_match_vault_manifest")
    if any(item["status"] != "verified" or item["integrity"] != "verified" for item in manifest):
        problems.append("artifact_not_verified")
    if job["status"] != "completed":
        problems.append("job_not_completed")
    if audit["status"] != "complete_within_scope" or audit.get("gaps"):
        problems.append("independent_audit_has_gaps")
    current_tasks = [item for item in tasks if item["revision"] == job["revision"] and item["generation"] == job["generation"]]
    if any(item["state"] != "succeeded" for item in current_tasks):
        problems.append("unfinished_or_failed_tasks")
    task_kinds = {item["id"]: item["kind"] for item in tasks}
    route_counts = Counter((event["payload"].get("model"), event["payload"].get("effort"), task_kinds.get(event["payload"].get("task_id")))
                           for event in events if event["type"] == "model_selected")
    routes = [{"model": key[0], "effort": key[1], "kind": key[2], "count": count} for key, count in sorted(route_counts.items(), key=str)]
    failures = sum(event["type"] in ("tool_failed", "agent_failed") for event in events)
    output_manifest = {"included_resource_keys": included, "excluded_resource_keys": excluded,
                       "artifacts": [{key: item.get(key) for key in ("resource_key", "role", "sha256", "version")} for item in manifest]}
    return {"job_id": job_id, "job_status": job["status"], "revision": job["revision"], "generation": job["generation"],
            "protocol_digest": job["protocol_digest"], "semantic_input_digest": canonical_digest(_semantic_mission(job["mission"])),
            "audit": audit, "quality_pass": not problems, "quality_failures": problems,
            "missing_resource_keys": missing, "unexpected_resource_keys": unexpected,
            "missing_artifacts": missing_artifacts, "unexpected_artifacts": unexpected_artifacts,
            "artifact_hash_failures": hash_failures, "manifest": output_manifest,
            "output_manifest_digest": canonical_digest(output_manifest), "artifacts": manifest,
            "failure_count": failures, "task_failure_count": sum(item["state"] in ("blocked", "failed") for item in current_tasks),
            "observed_routes": routes, "usage": _usage(events), "event_digest": canonical_digest(events),
            "global_recall": "unknown", "human_intervention_seconds": None,
            "http_request_count": None, "notes": ["Missing usage is unknown, never zero", "Quality compares declared finite reference sets only"]}


def _report_path(state_dir, report_ref):
    root = (Path(state_dir) / "reports").resolve()
    value = Path(report_ref)
    path = value.resolve() if value.is_absolute() else (root / value).resolve()
    if not path.is_relative_to(root) or path.suffix != ".json" or path == root:
        raise EvaluationError("Evaluation reports must be JSON files inside state/reports")
    return path


def load_evaluation_report(state_dir, report_ref, expected_sha256=None) -> dict:
    path = _report_path(state_dir, report_ref)
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise EvaluationError("Evaluation report exceeds size limit")
        data = path.read_bytes()
    except OSError:
        raise EvaluationError("Evaluation report is unavailable") from None
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise EvaluationError("Evaluation report SHA-256 mismatch")
    try:
        report = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise EvaluationError("Evaluation report is not valid JSON") from None
    if not isinstance(report, dict):
        raise EvaluationError("Evaluation report must be an object")
    return {"report": report, "report_ref": str(path.relative_to((Path(state_dir) / "reports").resolve())), "report_sha256": digest}


def _validate_routing_profile(profile, *, state_dir, protocol_digest, kind, fixture_ids=None) -> dict:
    """Only a bound empirical report can authorize the explicitly tested downgrade.

An operator's editable `passed: true` flag is never sufficient. This is a local
provenance/quality gate, not a cryptographic attestation by a model provider.
"""
    reasons = []
    if not isinstance(profile, dict):
        return {"valid": False, "reasons": ["routing_profile_missing"], "profile": None}
    required = ("report_ref", "report_sha256", "protocol_digest", "kind", "fixture_ids", "model", "effort")
    if any(key not in profile for key in required):
        return {"valid": False, "reasons": ["routing_profile_incomplete"], "profile": None}
    if not isinstance(profile["report_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", profile["report_sha256"]):
        return {"valid": False, "reasons": ["valid_report_sha256_required"], "profile": None}
    target = DOWNGRADE_TARGETS.get(kind)
    if target is None or (profile["model"], profile["effort"]) != target:
        reasons.append("unsupported_downgrade_target")
    if profile["protocol_digest"] != protocol_digest or profile["kind"] != kind:
        reasons.append("profile_protocol_or_kind_mismatch")
    try:
        loaded = load_evaluation_report(state_dir, profile["report_ref"], profile["report_sha256"])
    except (EvaluationError, TypeError, ValueError) as exc:
        return {"valid": False, "reasons": reasons + [str(exc)], "profile": None}
    report = loaded["report"]
    comparisons = {"fixed_astra_vs_auto_same_runtime", "fixed_astra_vs_experimental_profile_same_runtime"}
    if report.get("schema_version") != SCHEMA_VERSION or report.get("comparison") not in comparisons:
        reasons.append("unsupported_report_contract")
    if report.get("comparison") == "fixed_astra_vs_experimental_profile_same_runtime":
        candidate = report.get("routed_policy") or {}
        if candidate.get("mode") != "experimental_profile" or (candidate.get("model"), candidate.get("effort")) != target:
            reasons.append("experimental_profile_does_not_match_target")
    if report.get("evidence_kind") != "empirical" or report.get("synthetic") is not False:
        reasons.append("empirical_evidence_required")
    if report.get("protocol_digest") != protocol_digest or report.get("kind") != kind:
        reasons.append("report_protocol_or_kind_mismatch")
    cases = report.get("cases")
    if not isinstance(cases, list):
        cases = []
        reasons.append("paired_cases_missing")
    ids = [case.get("fixture_id") for case in cases if isinstance(case, dict)]
    if len(cases) < 5 or len(ids) != len(cases) or any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        reasons.append("at_least_five_distinct_paired_cases_required")
    if not isinstance(report.get("fixture_ids"), list) or set(report["fixture_ids"]) != set(ids) or len(report["fixture_ids"]) != len(ids):
        reasons.append("report_fixture_ids_do_not_match_cases")
    if len({(case.get("fixture_sha256"), case.get("semantic_input_digest")) for case in cases if isinstance(case, dict)}) < 5:
        reasons.append("five_distinct_fixture_input_pairs_required")
    if not isinstance(profile["fixture_ids"], list) or set(ids) != set(profile["fixture_ids"]) or len(ids) != len(profile["fixture_ids"]):
        reasons.append("fixture_ids_do_not_match_profile")
    if fixture_ids is not None and (set(ids) != set(fixture_ids) or len(ids) != len(fixture_ids)):
        reasons.append("fixture_ids_do_not_match_requested_validation_set")
    baseline_failures = routed_failures = 0
    for case in cases:
        if not isinstance(case, dict):
            reasons.append("invalid_paired_case")
            continue
        baseline, routed = case.get("baseline", {}), case.get("routed", {})
        if not isinstance(baseline, dict) or not isinstance(routed, dict):
            reasons.append("missing_pair_arm")
            continue
        expected_digest = case.get("semantic_input_digest")
        if not isinstance(case.get("mission"), dict) or canonical_digest(_semantic_mission(case["mission"])) != expected_digest:
            reasons.append("reference_mission_digest_mismatch")
        if canonical_digest(case.get("rune")) != protocol_digest:
            reasons.append("reference_rune_digest_mismatch")
        expectation = case.get("expectation")
        if not isinstance(expectation, dict) or canonical_digest(expectation) != case.get("expectation_digest"):
            reasons.append("reference_expectation_digest_mismatch")
            expectation = {}
        if not expected_digest or baseline.get("semantic_input_digest") != expected_digest or routed.get("semantic_input_digest") != expected_digest:
            reasons.append("paired_inputs_differ")
        if not case.get("fixture_sha256") or not case.get("expectation_digest"):
            reasons.append("fixture_and_reference_digests_required")
        if baseline.get("protocol_digest") != protocol_digest or routed.get("protocol_digest") != protocol_digest:
            reasons.append("paired_protocols_differ")
        if baseline.get("quality_pass") is not True or routed.get("quality_pass") is not True:
            reasons.append("both_arms_must_pass_quality")
        if baseline.get("output_manifest_digest") != routed.get("output_manifest_digest") or not baseline.get("output_manifest_digest"):
            reasons.append("paired_output_invariants_differ")
        for arm in (baseline, routed):
            manifest = arm.get("manifest") or {}
            if canonical_digest(manifest) != arm.get("output_manifest_digest"):
                reasons.append("output_manifest_digest_mismatch")
            if (sorted(manifest.get("included_resource_keys", [])) != sorted(expectation.get("included_resource_keys", []))
                    or sorted(manifest.get("excluded_resource_keys", [])) != sorted(expectation.get("excluded_resource_keys", []))
                    or Counter(_artifact_key(item) for item in manifest.get("artifacts", [])) != Counter(_artifact_key(item) for item in expectation.get("artifacts", []))):
                reasons.append("output_disagrees_with_independent_expectation")
            if Counter(_artifact_key(item) for item in arm.get("artifacts", [])) != Counter(_artifact_key(item) for item in manifest.get("artifacts", [])):
                reasons.append("artifact_measurements_do_not_match_manifest")
            if any(item.get("hash_matches") is not True or item.get("recomputed_sha256") != item.get("sha256")
                   or item.get("status") != "verified" or item.get("integrity") != "verified" for item in arm.get("artifacts", [])):
                reasons.append("artifact_validation_failed")
            if arm.get("quality_failures") or arm.get("audit", {}).get("status") != "complete_within_scope" or arm.get("audit", {}).get("gaps"):
                reasons.append("quality_flag_disagrees_with_audit")
            if arm.get("job_status") != "completed" or arm.get("usage", {}).get("decision_count", 0) < 1:
                reasons.append("completed_empirical_model_run_required")
        routes = baseline.get("observed_routes") or []
        if not routes or any(route.get("model") != BASELINE_MODEL or route.get("effort") != BASELINE_EFFORT for route in routes):
            reasons.append("baseline_was_not_fixed_astra_high")
        candidate_routes = routed.get("observed_routes", [])
        if not any(route.get("kind") == kind and (route.get("model"), route.get("effort")) == target and route.get("count", 0) > 0 for route in candidate_routes):
            reasons.append("claimed_downgrade_was_not_observed")
        if report.get("comparison") == "fixed_astra_vs_experimental_profile_same_runtime" and any(
                (route.get("model"), route.get("effort")) != target for route in candidate_routes):
            reasons.append("experimental_profile_was_not_fixed")
        left, right = baseline.get("failure_count"), routed.get("failure_count")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (left, right)):
            reasons.append("measured_failure_counts_required")
        else:
            baseline_failures += left
            routed_failures += right
            if right > left:
                reasons.append("routed_failure_count_exceeds_baseline")
    if report.get("runtime_changed_during_evaluation") is not False:
        reasons.append("runtime_equivalence_not_established")
    if (report.get("runtime") or {}).get("digest") != runtime_fingerprint()["digest"]:
        reasons.append("evaluation_runtime_differs_from_current_runtime")
    reasons = list(dict.fromkeys(reasons))
    validated = {**profile, "passed": True, "empirical_paired_cases": len(cases), "baseline_failures": baseline_failures,
                 "routed_failures": routed_failures, "validation_scope": "routing_quality_within_same_runtime",
                 "cost_savings_proven": False} if not reasons else None
    return {"valid": not reasons, "reasons": reasons, "profile": validated}


def validate_routing_profile(profile, *, state_dir, protocol_digest, kind, fixture_ids=None) -> dict:
    """Malformed or missing evidence always rejects the downgrade, never crashes routing."""
    try:
        return _validate_routing_profile(profile, state_dir=state_dir, protocol_digest=protocol_digest,
                                         kind=kind, fixture_ids=fixture_ids)
    except (TypeError, ValueError, KeyError, AttributeError, OSError):
        return {"valid": False, "reasons": ["malformed_validation_evidence"], "profile": None}


async def run_paired_evaluation(settings: Settings, cases: list[dict], *, report_name="paired-evaluation.json",
                                engine_factory: Callable | None = None, timeout_seconds=600, poll_interval=0.1,
                                candidate_profile: dict | None = None, access_profile: dict | None = None,
                                on_progress: Callable | None = None) -> dict:
    """Run baseline/routed arms sequentially with identical finite ground truth.

Custom factories are explicitly labeled synthetic for deterministic offline tests.
Auto may retain Astra until independent downgrade validation exists; such a run
cannot authorize a lower model it never actually exercised.
An explicit candidate_profile runs a fixed experimental candidate, never claims
production auto selected it, and does not manufacture a passed routing profile.
"""
    from .engine import Engine
    factory = engine_factory or Engine
    if not cases or timeout_seconds <= 0 or not 0 < poll_interval <= 5:
        raise EvaluationError("Cases and positive bounded timing settings are required")
    validated_cases = []
    for original in cases:
        case = deepcopy(original)
        if not isinstance(case.get("fixture_id"), str) or not case["fixture_id"]:
            raise EvaluationError("Each case requires a stable fixture_id")
        if not isinstance(case.get("fixture_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", case["fixture_sha256"]):
            raise EvaluationError("Each case requires a fixture SHA-256")
        if not isinstance(case.get("expectation"), dict):
            raise EvaluationError("Independent finite expectation is required before execution")
        expected = case["expectation"]
        for key in ("included_resource_keys", "excluded_resource_keys", "artifacts"):
            if not isinstance(expected.get(key), list):
                raise EvaluationError("Expectation must declare included/excluded keys and artifacts before execution")
        for key in ("included_resource_keys", "excluded_resource_keys"):
            if any(not isinstance(item, str) or not item for item in expected[key]) or len(set(expected[key])) != len(expected[key]):
                raise EvaluationError("Expected resource keys must be nonempty, distinct strings")
        if set(expected["included_resource_keys"]) & set(expected["excluded_resource_keys"]):
            raise EvaluationError("A reference resource cannot be both included and excluded")
        for artifact in expected["artifacts"]:
            if (not isinstance(artifact, dict) or artifact.get("resource_key") not in expected["included_resource_keys"]
                    or not isinstance(artifact.get("role"), str) or not artifact["role"]
                    or not isinstance(artifact.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])):
                raise EvaluationError("Every expected artifact must bind an included resource, role and SHA-256")
        if case.get("kind", "retrieve") not in DOWNGRADE_TARGETS:
            raise EvaluationError("Evaluation kind must be retrieve, extract or classify")
        if "rune" not in case:
            raise EvaluationError("Each case requires its exact Rune contract")
        case["rune"] = Rune.model_validate(case["rune"]).model_dump(mode="json", exclude_none=True)
        case["kind"] = case.get("kind", "retrieve")
        case["mission"] = _semantic_mission(case["mission"])
        provider = case["mission"].get("backend", "codex")
        if provider != "codex" and not (isinstance(provider, dict) and provider.get("kind") == "codex"):
            raise EvaluationError("This paired comparison requires the same Codex backend")
        case["semantic_input_digest"] = canonical_digest(case["mission"])
        case["expectation_digest"] = canonical_digest(case["expectation"])
        case["protocol_digest"] = canonical_digest(case["rune"])
        validated_cases.append(case)
    if len({case["fixture_id"] for case in validated_cases}) != len(cases):
        raise EvaluationError("Duplicate fixture IDs are not independent paired cases")
    protocols = {case["protocol_digest"] for case in validated_cases}
    kinds = {case["kind"] for case in validated_cases}
    if len(protocols) != 1 or len(kinds) != 1:
        raise EvaluationError("A report binds one Rune digest and one task kind")
    candidate_profile = deepcopy(candidate_profile)
    if candidate_profile is not None and (not isinstance(candidate_profile, dict)
            or set(candidate_profile) != {"model", "effort"}
            or (candidate_profile["model"], candidate_profile["effort"]) != DOWNGRADE_TARGETS[next(iter(kinds))]):
        raise EvaluationError("Experimental candidate must declare the supported model and effort for this kind")
    access_profile = deepcopy(access_profile)
    if access_profile is not None and (not isinstance(access_profile, dict) or not access_profile.get("id")):
        raise EvaluationError("Explicit shared access setup requires a named access profile")
    path = _report_path(settings.state_dir, report_name)
    evaluation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    isolated = Path(settings.state_dir).resolve() / "evaluations" / evaluation_id
    before = runtime_fingerprint()
    synthetic = factory is not Engine
    comparison = "fixed_astra_vs_experimental_profile_same_runtime" if candidate_profile else "fixed_astra_vs_auto_same_runtime"
    report = {"schema_version": SCHEMA_VERSION, "comparison": comparison, "evaluation_id": evaluation_id,
              "evidence_kind": "synthetic_fixture" if synthetic else "empirical", "synthetic": synthetic,
              "started_at": _now(), "runtime": before, "protocol_digest": next(iter(protocols)), "kind": next(iter(kinds)),
              "fixture_ids": [case["fixture_id"] for case in validated_cases], "cases": [],
              "baseline_policy": {"mode": "fixed", "model": BASELINE_MODEL, "effort": BASELINE_EFFORT},
              "routed_policy": {"mode": "experimental_profile", **candidate_profile} if candidate_profile else {"mode": "quality_constrained_auto"},
              "access_profile_digest": canonical_digest(access_profile) if access_profile else None, "state_dir": str(isolated),
              "comparison_scope": "routing only; same Engine, tools, Rune, inputs, reference set and worker budget",
              "architecture_superiority_tested": False, "cost_savings_proven": False,
              "monetary_cost": None, "price_schedule": None, "setup_seconds": None}
    setup_started = time.monotonic()
    setup_seconds = 0.0
    for index, case in enumerate(validated_cases):
        entry = {key: case[key] for key in ("fixture_id", "fixture_sha256", "kind", "semantic_input_digest", "expectation_digest", "protocol_digest")}
        entry.update(mission=case["mission"], declared_mission=case["mission"], declared_input_digest=case["semantic_input_digest"],
                     rune=case["rune"], expectation=case["expectation"])
        entry["source_state"] = case.get("source_state", {"mode": "unspecified"})
        entry["order"] = ["baseline", "routed"] if index % 2 == 0 else ["routed", "baseline"]
        for arm in entry["order"]:
            prepared = time.monotonic()
            arm_state = isolated / f"case-{index:04d}" / arm
            arm_settings = settings.model_copy(update={"state_dir": arm_state, "database_url": f"sqlite:///{arm_state / 'ore.db'}",
                                                       "auth_token": secrets.token_urlsafe(32)})
            engine = factory(arm_settings)
            try:
                if access_profile is not None:
                    engine.save_profile(deepcopy(access_profile))
                mission = deepcopy(case["mission"])
                selected = {"model": BASELINE_MODEL, "effort": BASELINE_EFFORT} if arm == "baseline" else candidate_profile
                mission.update(model_policy="fixed" if selected else "quality_constrained_auto",
                               model=selected["model"] if selected else None, effort=selected["effort"] if selected else None)
                job = engine.create(mission, case["rune"])
                if arm == entry["order"][0]:
                    entry["mission"] = _semantic_mission(job["mission"])
                    entry["semantic_input_digest"] = canonical_digest(entry["mission"])
                if on_progress:
                    on_progress({"fixture_id": case["fixture_id"], "arm": arm, "status": "started", "job_id": job["id"]})
                # Both arms exclude an identical deterministic setup prefix. Fresh
                # state per arm prevents another worker claiming this setup task.
                preparation = engine.store.claim_task("evaluation-setup", job_id=job["id"])
                engine.store.finish_task(preparation["id"], "evaluation-setup", preparation["fence"],
                    {"setup": "same bounded task initialized for both arms; no model decision"}, preparation["revision"])
                engine.store.create_task(job["id"], case["kind"], {"goal": mission["goal"], "urls": mission.get("urls", [])}, "evaluation-case")
                setup_seconds += time.monotonic() - prepared
                started = time.monotonic()
                await engine.run(job["id"])
                timed_out = False
                while engine.store.get_job(job["id"])["status"] not in TERMINAL:
                    if time.monotonic() - started >= timeout_seconds:
                        timed_out = True
                        await engine.pause(job["id"])
                        break
                    await asyncio.sleep(poll_interval)
                elapsed = time.monotonic() - started
                measured = time.monotonic()
                metrics = evaluate_job(engine, job["id"], case["expectation"])
                entry[arm] = {**metrics, "execution_seconds": elapsed, "measurement_seconds": time.monotonic() - measured,
                              "timeout": timed_out, "state_dir": str(arm_state),
                              "policy": report["baseline_policy" if arm == "baseline" else "routed_policy"]}
                if on_progress:
                    on_progress({"fixture_id": case["fixture_id"], "arm": arm, "status": metrics["job_status"],
                                 "quality_pass": metrics["quality_pass"], "execution_seconds": elapsed})
            finally:
                await engine.stop()
        report["cases"].append(entry)
    report["setup_seconds"] = setup_seconds
    report["finished_at"] = _now()
    report["total_wall_seconds"] = time.monotonic() - setup_started
    report["runtime_changed_during_evaluation"] = before["digest"] != runtime_fingerprint()["digest"]
    report["quality_pass"] = bool(report["cases"]) and all(case[arm]["quality_pass"] for case in report["cases"] for arm in ("baseline", "routed"))
    report["paired_outputs_equal"] = all(case["baseline"]["output_manifest_digest"] == case["routed"]["output_manifest_digest"] for case in report["cases"])
    report["limitations"] = ["No plain-agent baseline; cannot establish architectural superiority", "Unknown global search recall",
                              "No token-price or monetary savings proof without complete measured usage and an applicable price schedule",
                              "Unvalidated automatic policy may choose Astra in both arms; no downgrade is certified without observing its actual model/effort",
                              "Live source state, caches, quotas, human interventions and preparation costs can confound elapsed-time comparisons"]
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return {**report, "report_ref": str(path.relative_to((Path(settings.state_dir) / "reports").resolve())),
            "report_sha256": hashlib.sha256(encoded).hexdigest()}
