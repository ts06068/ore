"""Planner recovery messages must be useful without leaking provider payloads."""
import json

import httpx
import pytest
from test_conversation import decision, plan, turn
from test_conversation import setup as conversation_setup

from ore.codex import BackendError
from ore.conversation_errors import planner_failure
from ore.run_budget import BudgetExhausted

setup = conversation_setup


@pytest.mark.parametrize("code, phrase", [
    ("budget_token_exhausted", "token budget"),
    ("budget_turn_exhausted", "model-turn budget"),
    ("budget_time_exhausted", "time budget"),
    ("budget_bytes_exhausted", "download-size budget"),
    ("budget_accounting_unavailable", "usage accounting"),
    ("budget_paused", "paused"),
])
def test_budget_failures_explain_limit_without_exposing_ledger(code, phrase):
    result = planner_failure(BudgetExhausted(code, {"provider_token": "DO_NOT_EXPOSE"}))
    assert result["code"] == code and phrase in result["message"]
    assert result["action"]
    assert "DO_NOT_EXPOSE" not in json.dumps(result)


@pytest.mark.parametrize("status, code", [
    (401, "planner_authentication_required"), (403, "planner_access_denied"),
    (429, "planner_rate_limited"), (503, "planner_service_unavailable"),
])
def test_http_failure_does_not_expose_request_url_headers_or_body(status, code):
    request = httpx.Request("POST", "https://provider.example/private?api_key=DO_NOT_EXPOSE",
                            headers={"Authorization": "Bearer DO_NOT_EXPOSE"})
    response = httpx.Response(status, request=request, text="DO_NOT_EXPOSE")
    result = planner_failure(httpx.HTTPStatusError("DO_NOT_EXPOSE", request=request, response=response))
    assert result["code"] == code and result["action"]
    assert "DO_NOT_EXPOSE" not in json.dumps(result)
    assert "provider.example" not in json.dumps(result)


@pytest.mark.parametrize("detail, code", [
    ("Not logged in: DO_NOT_EXPOSE", "planner_authentication_required"),
    ("Your usage limit has been reached DO_NOT_EXPOSE", "planner_rate_limited"),
    ("Codex transport closed DO_NOT_EXPOSE", "planner_backend_failed"),
])
def test_provider_errors_use_controlled_messages(detail, code):
    result = planner_failure(BackendError(detail))
    assert result["code"] == code
    assert "DO_NOT_EXPOSE" not in json.dumps(result)


@pytest.mark.parametrize("error, code", [
    (httpx.ReadTimeout("DO_NOT_EXPOSE"), "planner_service_timeout"),
    (TimeoutError("DO_NOT_EXPOSE"), "planner_timeout"),
    (httpx.ConnectError("DO_NOT_EXPOSE"), "planner_service_unavailable"),
])
def test_network_errors_have_recovery_action(error, code):
    result = planner_failure(error)
    assert result["code"] == code and result["action"]
    assert "DO_NOT_EXPOSE" not in json.dumps(result)


@pytest.mark.parametrize("error, code", [
    (BudgetExhausted("budget_token_exhausted", {}), "budget_token_exhausted"),
    (BackendError("Not logged in: DO_NOT_EXPOSE"), "planner_authentication_required"),
    (BackendError("rate limit reached DO_NOT_EXPOSE"), "planner_rate_limited"),
])
async def test_failed_turn_preserves_prior_plan_and_publishes_recovery(setup, monkeypatch, error, code):
    engine, manager = setup
    ident = manager.create()["id"]
    engine.backend.responses = [decision("propose_plan", **plan())]
    initial = await turn(manager, ident, "Prepare extraction")
    previous = initial["plans"]
    async def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(manager, "_decision", fail)
    result = await turn(manager, ident, "Continue preparing the plan")
    assert result["status"] == "error" and not result["planner_running"]
    assert result["plans"] == previous and result["runs"] == []
    failure = [event["data"] for event in manager.events(ident) if event["type"] == "error"][-1]
    assert failure["code"] == code and failure["action"]
    assert "DO_NOT_EXPOSE" not in json.dumps(result)
