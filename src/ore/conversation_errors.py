"""Controlled planner failure messages; raw provider bodies never enter the chat."""
from __future__ import annotations

import httpx

from .codex import BackendError
from .policy import AccessDenied
from .run_budget import BudgetExhausted

_PRESERVED = " Your conversation and previous plan are preserved."


def planner_failure(exc: Exception) -> dict:
    """Classify a failure for recovery without exposing credentials or provider text."""
    if isinstance(exc, BudgetExhausted):
        descriptions = {
            "budget_token_exhausted": ("Planning reached this request's token budget.", "Start a new conversation with a larger token budget or a narrower scope."),
            "budget_turn_exhausted": ("Planning reached this request's model-turn budget.", "Start a new conversation with a larger turn budget or a narrower scope."),
            "budget_time_exhausted": ("Planning reached this request's time budget.", "Start a new conversation with a larger time budget or a narrower scope."),
            "budget_bytes_exhausted": ("This request reached its download-size budget.", "Start a new conversation with a larger download budget or fewer artifacts."),
            "budget_accounting_unavailable": ("Planning paused because the provider did not confirm complete usage accounting.", "Check the model connection before starting a new conversation; usage is not assumed to be zero."),
            "budget_paused": ("This request's execution budget is paused.", "Resume the paused request when you are ready to continue."),
        }
        message, action = descriptions.get(exc.code, ("This request reached an execution budget limit.", "Review the request's budget before trying again."))
        return {"code": exc.code, "message": message + _PRESERVED, "action": action}

    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    # Provider RPC errors can contain sensitive body text. Inspect only locally
    # for known classes, and return fixed messages rather than redacting a body.
    detail = str(exc).casefold() if isinstance(exc, (BackendError, AccessDenied)) else ""
    if status == 401 or any(marker in detail for marker in (
            "authentication required", "not authenticated", "not logged in", "unauthorized",
            "invalid api key", "invalid_api_key", "token expired", "token has expired",
            "credential is unavailable", "sign in", "sign-in", "login required")):
        return {"code": "planner_authentication_required",
                "message": "Planning needs a valid service connection." + _PRESERVED,
                "action": "Open Connections in this chat to sign in or update the relevant API key, then retry."}
    if status == 429 or any(marker in detail for marker in (
            "rate limit", "rate_limit", "usage limit", "usage_limit", "quota exceeded", "insufficient_quota")):
        return {"code": "planner_rate_limited",
                "message": "A service paused planning because its rate or usage limit was reached." + _PRESERVED,
                "action": "Check the connection's usage limit and retry after its reset; retrying now does not increase the limit."}
    if status == 403:
        return {"code": "planner_access_denied",
                "message": "A service denied access needed for planning." + _PRESERVED,
                "action": "Check the connection's permissions and authorized network, or choose an accessible source."}
    if isinstance(exc, TimeoutError):
        return {"code": "planner_timeout",
                "message": "Planning reached its time limit." + _PRESERVED,
                "action": "Retry planning with a narrower scope or a larger time budget."}
    if isinstance(exc, httpx.TimeoutException):
        return {"code": "planner_service_timeout",
                "message": "A service did not respond before the planning timeout." + _PRESERVED,
                "action": "Check the service connection and retry this turn."}
    if isinstance(exc, httpx.TransportError) or status is not None and status >= 500:
        return {"code": "planner_service_unavailable",
                "message": "A service needed for planning is temporarily unavailable." + _PRESERVED,
                "action": "Check the service connection and retry after it becomes available."}
    if isinstance(exc, BackendError):
        return {"code": "planner_backend_failed",
                "message": "The model connection ended before planning completed." + _PRESERVED,
                "action": "Check the selected model and its connection, then retry this turn."}
    return {"code": type(exc).__name__,
            "message": "The planner could not complete this turn." + _PRESERVED,
            "action": "Retry this turn. If it fails again, use the error code when reporting the problem."}
