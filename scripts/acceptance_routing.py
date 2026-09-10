"""Verify production auto selects Terra after a bound empirical extraction gate.

Only the known public paragraph fixture is served. All job, profile and browser
state is fresh. Calibration report bytes are copied without modification so the
normal Engine validator can resolve the same report within its isolated state.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time

from ore.engine import Engine
from ore.evaluation import evaluate_job, load_evaluation_report, runtime_fingerprint, validate_routing_profile
from calibrate_routing import browser_settings, build_cases, fixture_documents


async def run(state_dir, calibration_report, output_name):
    root = Path(state_dir).resolve()
    validation_file = root / "reports" / (Path(calibration_report).stem + "-validation.json")
    validation = json.loads(validation_file.read_text())
    if not validation.get("valid"):
        raise RuntimeError("Calibration did not validate")
    profile = validation["profile"]
    loaded = load_evaluation_report(root, profile["report_ref"], profile["report_sha256"])
    report = loaded["report"]
    current_runtime = runtime_fingerprint()
    if current_runtime["digest"] != report["runtime"]["digest"]:
        raise RuntimeError("Runtime changed since calibration; repeat empirical calibration first")
    checked = validate_routing_profile(profile, state_dir=root, protocol_digest=report["protocol_digest"],
                                      kind="extract", fixture_ids=report["fixture_ids"])
    if not checked["valid"]:
        raise RuntimeError("Calibration evidence gate rejected the profile")
    isolated = root / "acceptance" / "routing" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3))
    isolated_reports = isolated / "reports"
    isolated_reports.mkdir(parents=True, mode=0o700)
    copied = isolated_reports / Path(profile["report_ref"]).name
    copied.write_bytes((root / "reports" / profile["report_ref"]).read_bytes())
    profile = {**profile, "report_ref": copied.name}
    documents = fixture_documents()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = documents.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = browser_settings(root).model_copy(update={"state_dir": isolated, "database_url": f"sqlite:///{isolated / 'ore.db'}", "auth_token": secrets.token_urlsafe(32)})
    engine = Engine(settings)
    started = time.monotonic()
    try:
        access = {"id": "validated-public-local", "allowed_hosts": ["127.0.0.1"], "routing_validation": profile}
        engine.save_profile(access)
        case = build_cases(f"http://127.0.0.1:{server.server_port}")[0]
        mission = {**case["mission"], "access_profile_ref": access["id"], "routing": {"mode": "quality_constrained_auto"}}
        job = engine.create(mission, case["rune"])
        preparation = engine.store.claim_task("auto-acceptance-setup", job_id=job["id"])
        engine.store.finish_task(preparation["id"], "auto-acceptance-setup", preparation["fence"],
                                {"setup": "deterministic bounded extract task; no planning model call"}, preparation["revision"])
        engine.store.create_task(job["id"], "extract", {"goal": mission["goal"], "urls": mission["urls"]}, "automatic-extraction-proof")
        print(json.dumps({"status": "started", "job_id": job["id"], "mode": "quality_constrained_auto"}), flush=True)
        await engine.run(job["id"])
        timed_out = False
        while engine.store.get_job(job["id"])["status"] in ("queued", "running"):
            if time.monotonic() - started > 50:
                timed_out = True
                await engine.pause(job["id"])
                break
            await asyncio.sleep(0.2)
        metrics = evaluate_job(engine, job["id"], case["expectation"])
        actual = metrics["observed_routes"]
        selected = bool(actual) and all(row["model"] == "gpt-5.6-terra" and row["effort"] == "low" and row["kind"] == "extract" and row["count"] > 0 for row in actual)
        unchanged = runtime_fingerprint()["digest"] == current_runtime["digest"]
        passed = metrics["quality_pass"] and selected and not timed_out and unchanged and metrics["protocol_digest"] == report["protocol_digest"]
        result = {"schema_version": "ore.automatic-routing-acceptance/v1", "passed": passed, "evidence_kind": "empirical",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "routing_policy": "quality_constrained_auto", "automatic_downgrade_observed": selected,
            "calibration_report_ref": loaded["report_ref"], "calibration_report_sha256": loaded["report_sha256"],
            "validated_fixture_ids": profile["fixture_ids"], "fixture_id": case["fixture_id"], "fixture_sha256": case["fixture_sha256"],
            "expectation": case["expectation"], "source_state": case["source_state"], "rune": engine.store.get_job(job["id"])["rune"],
            "runtime": current_runtime, "runtime_unchanged": unchanged, "elapsed_seconds": time.monotonic() - started,
            "timeout": timed_out, "state_dir": str(isolated), "metrics": metrics,
            "limitations": ["One known local extraction fixture only", "No architecture-superiority or monetary-savings claim", "No scholarly access or supplement claim"]}
        output = root / "reports" / output_name
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"passed": passed, "report": str(output), "job_id": job["id"], "routes": actual,
            "quality_pass": metrics["quality_pass"], "elapsed_seconds": result["elapsed_seconds"]}), flush=True)
        return 0 if passed else 1
    finally:
        await engine.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=".ore")
    parser.add_argument("--calibration-report", default="routing-extract-calibration-final.json")
    parser.add_argument("--output-name", default="routing-auto-acceptance.json")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.state_dir, args.calibration_report, args.output_name)))
