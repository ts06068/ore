#!/usr/bin/env python3
"""Real public OA binary acceptance through Engine/ToolRuntime; no model or login.

Artifacts, SQLite evidence and an isolated empty secret store remain under a fresh
acceptance state directory. This script never loads the operator onboarding state.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import time
from urllib.parse import unquote, urlsplit

from ore.config import Settings
from ore.engine import Engine
from ore.tools import ToolRuntime


TARGETS = [
    {"name": "Healthcare Informatics Research", "identifier": "PMC11333818", "doi": "10.4258/hir.2024.30.3.266"},
    {"name": "PLOS Medicine", "identifier": "10.1371/journal.pmed.1004493", "doi": "10.1371/journal.pmed.1004493"},
]


def utc():
    return datetime.now(timezone.utc).isoformat()


def file_digest(path, algorithm):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


async def validate_article(engine, target):
    started = time.monotonic()
    result = {**target, "started_at": utc(), "artifacts": [], "issues": [], "passed": False}
    job = engine.create({
        "goal": f"Verify public main PDF and every explicit JATS supplement for DOI {target['doi']}",
        "artifact_roles": ["main_pdf", "supplement"], "completeness": "bounded",
        "access_profile_ref": "public", "allowed_origins": ["https://pmc-oa-opendata.s3.amazonaws.com"],
        "budget": {"max_turns": 1, "max_seconds": 180, "max_agent_workers": 1, "max_bytes": 64 * 1024 * 1024},
        "limits": {"origin_min_interval_seconds": 1, "max_artifact_bytes": 32 * 1024 * 1024},
        "acceptance_mode": "manual_toolruntime_no_model",
    })
    task = engine.store.claim_task("oa-acceptance", lease_seconds=180, job_id=job["id"])
    runtime = ToolRuntime(engine, job["id"], task)
    result["job_id"], result["task_id"] = job["id"], task["id"]
    try:
        resolved = await runtime.execute("resolve", {"source": "pmc", "identifier": target["identifier"]})
        result["resolver"] = resolved
        main = [item for item in resolved["candidates"] if item["role"] == "main_pdf"]
        supplements = [item for item in resolved["candidates"] if item["role"] == "supplement"]
        result["discovered_main_count"] = len(main)
        result["discovered_supplement_count"] = len(supplements)
        if len(main) != 1:
            raise ValueError(f"Expected exactly one main PDF in the selected version; observed {len(main)}")
        if main[0].get("doi", "").lower() != target["doi"].lower():
            raise ValueError("Resolver main candidate DOI does not match the target")
        if not supplements:
            raise ValueError("This acceptance target requires explicit supplements; none were discovered")
        if resolved.get("issues"):
            result["issues"].append({"phase": "discovery", "details": resolved["issues"]})
        if any(item.get("external_reference") for item in supplements):
            raise ValueError("External supplement needs an explicit additional origin/identity inspection")
        resource = await runtime.execute("resource", {
            "id": "doi:" + target["doi"], "doi": target["doi"], "title": main[0].get("title") or target["name"],
            "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{resolved['pmcid']}/",
            "classification": "included", "article_type": "original_article_acceptance_target",
            "version": main[0].get("version", "unknown"), "supplement_status": "present",
            "expected_supplements": len(supplements),
            "supplement_evidence": "Explicit supplementary-material relationships in the selected PMC JATS version; resolver observation is persisted",
            "reason": "User-authorized bounded OA binary acceptance target, not a whole-issue selection claim",
        })
        for candidate in [*main, *supplements]:
            filename = unquote(urlsplit(candidate["url"]).path.rsplit("/", 1)[-1])
            arguments = {"url": candidate["url"], "resource_id": resource["id"], "role": candidate["role"],
                         "filename": filename, "version": candidate.get("version", "unknown")}
            if candidate["role"] == "main_pdf":
                # The main PDF must contain the target DOI. Supplement identity is
                # established by its explicit parent link plus dataset checksum;
                # attachments need not repeat the article DOI in their content.
                arguments["doi"] = target["doi"]
            artifact = await runtime.execute("download", arguments)
            actual_sha256 = file_digest(artifact["path"], "sha256")
            expected_md5 = candidate.get("expected_md5")
            actual_md5 = file_digest(artifact["path"], "md5")
            evidence = {
                "role": candidate["role"], "filename": filename, "artifact_id": artifact["id"],
                "path": artifact["path"], "bytes": Path(artifact["path"]).stat().st_size,
                "media_type": artifact["media_type"], "status": artifact["status"],
                "integrity": artifact["integrity"], "identity": artifact["identity"],
                "identity_evidence": artifact.get("identity_evidence", {}),
                "sha256": actual_sha256, "sha256_matches_vault": actual_sha256 == artifact["sha256"],
                "md5": actual_md5, "expected_md5": expected_md5,
                "md5_matches_dataset": actual_md5.lower() == expected_md5.lower() if expected_md5 else None,
                "source_url": candidate["url"], "article_version": candidate.get("article_version"),
                "version": candidate.get("version"), "license": candidate.get("license"),
                "parent_doi": target["doi"], "relationship_evidence": candidate.get("relationship_evidence"),
                "archive_member_count": len(artifact.get("members", [])), "issues": artifact.get("issues", []),
            }
            result["artifacts"].append(evidence)
            engine.store.record_observation(job["id"], {"kind": "acceptance_binary_verification", "artifact_id": artifact["id"], "data": evidence}, lease=runtime.lease)
            if artifact["status"] != "verified" or not evidence["sha256_matches_vault"] or evidence["md5_matches_dataset"] is not True:
                result["issues"].append({"phase": "binary_validation", "filename": filename, "status": artifact["status"], "md5_match": evidence["md5_matches_dataset"]})
            if candidate["role"] == "main_pdf" and not artifact.get("identity_evidence", {}).get("doi_match"):
                result["issues"].append({"phase": "main_identity", "code": "target_doi_not_verified_in_pdf"})
            print(json.dumps({"article": target["name"], "role": candidate["role"], "filename": filename,
                              "bytes": evidence["bytes"], "status": artifact["status"], "md5_match": evidence["md5_matches_dataset"]}), flush=True)
        proposal = await runtime.execute("finish", {"summary": "Downloaded every selected-version main and explicit supplement candidate and compared actual bytes/checksums"})
        result["audit"] = proposal["audit"]
        result["passed"] = (not result["issues"] and proposal["audit"]["status"] == "complete_within_scope"
                            and len(result["artifacts"]) == len(main) + len(supplements))
        engine.store.finish_task(task["id"], task["worker_id"], task["fence"], {"acceptance_passed": result["passed"], "audit": result["audit"]}, task["revision"])
        engine.reconcile(job["id"])
        if not result["passed"]:
            engine.store.update_job(job["id"], status="needs_review")
        result["job_status"] = engine.store.get_job(job["id"])["status"]
    except Exception as exc:
        result["issues"].append({"phase": "execution", "type": type(exc).__name__, "message": str(exc)[:1000]})
        engine.store.update_job(job["id"], status="needs_review")
        result["job_status"] = "needs_review"
    result["finished_at"] = utc()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


async def run(report_path, state_root):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    state_dir = state_root / (stamp + "-" + secrets.token_hex(3))
    # Explicit state/database/token values prevent reading the operator's running
    # server files or inheriting its database URL from ORE_* settings.
    settings = Settings(state_dir=state_dir, database_url=f"sqlite:///{state_dir.resolve() / 'ore.db'}",
                        auth_token=secrets.token_urlsafe(32), max_workers=1)
    engine = Engine(settings)
    engine.save_profile({"id": "public", "name": "Isolated public OA acceptance", "api_interval": 1,
                         "sources": {"pmc": {"email_env": "ORE_ACCEPTANCE_UNUSED_CONTACT"}}})
    report = {"schema_version": "ore.acceptance/v1", "started_at": utc(), "mode": "real_binary_download_manual_toolruntime",
              "state_dir": str(state_dir.resolve()), "model_calls": 0, "onboarding_credentials_used": False,
              "scope": "Two specifically authorized OA articles; all explicit selected-version PMC supplements",
              "publication_version_boundary": "PMC integer version and manuscript flag are preserved; unknown is not relabeled publishedVersion",
              "targets": []}
    try:
        for target in TARGETS:
            report["targets"].append(await validate_article(engine, target))
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        await engine.stop()
    report["finished_at"] = utc()
    report["passed"] = len(report["targets"]) == len(TARGETS) and all(item["passed"] for item in report["targets"])
    report["artifact_count"] = sum(len(item["artifacts"]) for item in report["targets"])
    report["total_bytes"] = sum(artifact["bytes"] for item in report["targets"] for artifact in item["artifacts"])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(report_path.resolve()), "passed": report["passed"], "artifact_count": report["artifact_count"], "total_bytes": report["total_bytes"]}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=root / ".ore/reports/oa-acceptance.json")
    parser.add_argument("--state-root", type=Path, default=root / ".ore/acceptance/oa")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.report, args.state_root)) else 1)
