"""Run five real fixed Astra/high vs fixed Terra/low local extraction pairs.

Uses only generated, public fixture content. It neither reads onboarding secrets
nor installs a routing profile. A passing report can be reviewed and explicitly
bound to this exact Rune/task kind/fixture set by the operator.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

from ore.config import Settings
from ore.evaluation import run_paired_evaluation, validate_routing_profile

PARAGRAPHS = (
    "The observatory recorded 137 meteor trails during four clear nights.",
    "Water samples from six stations contained between 12 and 29 particles per liter.",
    "The archive contains 83 field notebooks dated from 1912 through 1937.",
    "At 18 degrees Celsius, the instrument reported a median delay of 2.75 seconds.",
    "연구팀은 서로 다른 세 지역에서 수집한 토양 표본 48개를 비교하였다.",
)
RUNE = {
    "protocol_id": "ore.calibration.exact-paragraph", "protocol_version": "1.0.0",
    "instructions": "For the one mission URL, register the declared resource as included, extract exactly the #target paragraph with page_extract, then finish. Use only observed page content and the returned resource and browser identifiers. Do not delegate or create extra artifacts.",
    "scope": {"artifact_roles": ["excerpt"]},
    "checks": ["Exactly one included resource", "Exactly one excerpt from CSS selector #target"],
    "limits": {"origin_min_interval_seconds": 0},
}
PROFILE = {"id": "calibration-public-local", "name": "Generated public local calibration pages", "allowed_hosts": ["127.0.0.1"]}


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture_documents():
    return {f"/fixture-{index}.html": (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>ORE public calibration fixture '
        + str(index) + '</title><h1>Field observation</h1><p id="target">' + paragraph
        + '</p><p id="unrelated">This footer is outside the requested paragraph.</p></html>'
    ).encode("utf-8") for index, paragraph in enumerate(PARAGRAPHS, 1)}


def build_cases(origin):
    documents = fixture_documents()
    cases = []
    for index, paragraph in enumerate(PARAGRAPHS, 1):
        fixture_id = f"ore-exact-paragraph-v1-{index}"
        route = f"/fixture-{index}.html"
        url = origin + route
        cases.append({"fixture_id": fixture_id, "fixture_sha256": sha(documents[route]), "kind": "extract", "rune": RUNE,
            "mission": {"goal": f"Open {url} using browser_open. Register exactly one included resource with explicit id {fixture_id}, title ORE calibration fixture {index}, and the observed page URL. Use page_extract for CSS selector #target with the returned resource id, session_id, and current epoch. Then finish. Do not delegate; do not create any other resources or artifacts.",
                "urls": [url], "artifact_roles": ["excerpt"], "access_profile_ref": PROFILE["id"],
                "budget": {"max_turns": 10, "max_seconds": 45, "max_agent_workers": 1},
                "limits": {"origin_min_interval_seconds": 0}},
            "expectation": {"included_resource_keys": [fixture_id], "excluded_resource_keys": [],
                "artifacts": [{"resource_key": fixture_id, "role": "excerpt", "sha256": sha(paragraph.encode("utf-8"))}]},
            "source_state": {"mode": "immutable_local_http_fixture", "encoding": "utf-8", "fixture_html": documents[route].decode("utf-8"),
                             "expected_excerpt": paragraph, "selector": "#target", "url": url}})
    return cases


def browser_settings(state_dir):
    # Sharing the installed executable/libraries does not share cookies or state.
    root = Path(state_dir).resolve()
    browsers = root / "browsers"
    if browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    libraries = root / "browser-libs" / "usr" / "lib" / "x86_64-linux-gnu"
    if libraries.is_dir():
        current = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = str(libraries) + (os.pathsep + current if current else "")
    executable = next(iter(sorted(browsers.glob("chromium-*/chrome-linux64/chrome"))), None)
    return Settings(state_dir=root, max_workers=1, browser_executable=str(executable) if executable else None)


async def run(state_dir, report_name):
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
    try:
        settings = browser_settings(state_dir)
        cases = build_cases(f"http://127.0.0.1:{server.server_port}")
        report = await run_paired_evaluation(settings, cases, report_name=report_name,
            candidate_profile={"model": "gpt-5.6-terra", "effort": "low"}, access_profile=PROFILE,
            timeout_seconds=50, poll_interval=0.2,
            on_progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        profile = {"report_ref": report["report_ref"], "report_sha256": report["report_sha256"],
            "protocol_digest": report["protocol_digest"], "kind": "extract", "fixture_ids": report["fixture_ids"],
            "model": "gpt-5.6-terra", "effort": "low"}
        validation = validate_routing_profile(profile, state_dir=settings.state_dir,
            protocol_digest=report["protocol_digest"], kind="extract", fixture_ids=report["fixture_ids"])
        validation_path = settings.state_dir / "reports" / (Path(report_name).stem + "-validation.json")
        validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report_ref": report["report_ref"], "report_sha256": report["report_sha256"],
            "quality_pass": report["quality_pass"], "paired_outputs_equal": report["paired_outputs_equal"],
            "total_wall_seconds": report["total_wall_seconds"], "runtime_changed_during_evaluation": report["runtime_changed_during_evaluation"],
            "validation": validation}, ensure_ascii=False), flush=True)
        return 0 if validation["valid"] else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=".ore")
    parser.add_argument("--report-name", default="routing-extract-calibration.json")
    options = parser.parse_args()
    raise SystemExit(asyncio.run(run(options.state_dir, options.report_name)))
