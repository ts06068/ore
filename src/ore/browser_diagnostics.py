"""Pure observation classification; never grants access or changes browser behavior.

Expected DNS/PAT observations describe one request, not a successful challenge.
The official debug page can report automation, but cannot establish why a
publisher challenged or blocked the same browser.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

TROUBLESHOOTING = "https://developers.cloudflare.com/cloudflare-challenges/troubleshooting/challenge-solve-issues/"
DNS_SOURCE = TROUBLESHOOTING + "#failed-subdomain-network-requests-during-turnstile-challenges"
PAT_SOURCE = TROUBLESHOOTING + "#401-response-on-a-private-access-token-request"
DEBUG_SOURCE = "https://debug.challenges.cloudflare.com/"
_PROBE_DOMAINS = ("challenges.cloudflare.com", "dnstest.dev")
_DNS_CODES = ("ERR_NAME_NOT_RESOLVED", "NS_ERROR_UNKNOWN_HOST", "DNS_PROBE_FINISHED_NXDOMAIN", "ENOTFOUND", "EAI_NONAME")
_OTHER_CODES = ("ERR_BLOCKED_BY_CLIENT", "ERR_BLOCKED_BY_RESPONSE", "ERR_ABORTED", "ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT", "ERR_DNS_TIMED_OUT", "ERR_CONNECTION_REFUSED", "ERR_FAILED")
_PAT_PATH = re.compile(r"^/cdn-cgi/challenge-platform/(?:[A-Za-z0-9_-]+/)+pat/[^/]+(?:/.*)?$")


def _url_parts(url):
    try:
        parts = urlsplit(str(url or ""))
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
        if parts.scheme not in ("http", "https") or not host or re.search(r"[\s/%\\]", host):
            return None
        if any(not label for label in host.split(".")):
            return None
        default = 443 if parts.scheme == "https" else 80
        display_host = "[" + host + "]" if ":" in host else host
        origin = parts.scheme + "://" + display_host + (":" + str(port) if port and port != default else "")
        return {"scheme": parts.scheme, "host": host, "port": port or default, "origin": origin, "path": parts.path}
    except (TypeError, ValueError):
        return None


def _probe_domain(host):
    return next((base for base in _PROBE_DOMAINS if host.endswith("." + base)), None)


def _error_evidence(error_code, error_reason):
    code = str(error_code or "")[:4096]
    reason = str(error_reason or "")[:4096]
    combined = re.sub(r"https?://[^\s]+", "", code + "\n" + reason, flags=re.I)
    # Policy rejection and timeouts are not the expected DNS lookup probes,
    # even if their contextual message also mentions a DNS error.
    disqualified = bool(re.search(r"blocked|scope|not allowed|denied|abort|timed.?out|timeout", combined, re.I))
    observed = next((item for item in (*_DNS_CODES, *_OTHER_CODES)
                     if re.search(r"(?<![A-Za-z0-9_])" + item + r"(?![A-Za-z0-9_])", combined, re.I)), None)
    phrase = bool(re.search(r"\b(?:name or service not known|nodename nor servname provided, or not known|could not resolve host|host(?:name| name) lookup (?:failed|failure)|dns (?:host |name )?lookup (?:failed|failure)|dns name resolution failed)\b", combined, re.I))
    dns = not disqualified and (observed in _DNS_CODES or phrase)
    return {"dns_lookup_failure": dns, "observed_error_code": observed,
            "error_present": bool(code or reason), "non_dns_block_or_timeout": disqualified}


def _is_pat_path(path):
    return bool(_PAT_PATH.fullmatch(path) and "\\" not in path and not {".", ".."}.intersection(path.split("/")))


def _evidence(url, parts, *, status=None, errors=None):
    base = _probe_domain(parts["host"]) if parts else None
    # Probe names and PAT paths can contain opaque identifiers. Preserve a
    # reference digest and structural evidence without exposing those values.
    origin = parts["origin"] if parts else None
    if base:
        origin = parts["scheme"] + "://[probe]." + base
    return {"url_origin": origin, "url_sha256": hashlib.sha256(str(url or "").encode()).hexdigest(),
            "url_valid": parts is not None, "probe_parent_domain": base,
            "path_kind": "challenge_platform_pat" if parts and _is_pat_path(parts["path"]) else "other",
            "http_status": status, **(errors or {}), "publisher_block_confirmed": False}


def _result(code, severity, expected, explanation, evidence, source_reference):
    return {"code": code, "severity": severity, "expected": expected,
            "actionable": severity == "warning", "explanation": explanation,
            "evidence": evidence, "source_reference": source_reference}


def classify_network_observation(url: str, *, status: int | None = None,
                                 error_code: str | None = None, error_reason: str | None = None,
                                 authorized_top_origin: str | None = None) -> dict:
    """Classify a recorded request; raw URLs, query strings and error text are omitted.

    ``authorized_top_origin`` explicitly permits classification of a same-origin
    challenge-platform PAT endpoint. It does not authorize any network request.
    Missing/other context is classified conservatively as an actionable failure.
    """
    parts = _url_parts(url)
    status = status if type(status) is int and 100 <= status <= 599 else None
    errors = _error_evidence(error_code, error_reason)
    evidence = _evidence(url, parts, status=status, errors=errors)
    if not parts:
        return _result("invalid_observation_url", "warning", False, "The recorded URL cannot be classified safely.", evidence, TROUBLESHOOTING)
    top = _url_parts(authorized_top_origin)
    recognized_pat_host = (parts["host"] == "challenges.cloudflare.com" and parts["port"] == 443
                           or top is not None and parts["origin"] == top["origin"])
    if (status == 401 and parts["scheme"] == "https" and recognized_pat_host
            and evidence["path_kind"] == "challenge_platform_pat" and not errors["error_present"]):
        return _result("expected_pat_unauthorized", "info", True,
            "This PAT endpoint returned an expected token-unavailable response; this response alone does not establish a challenge failure.", evidence, PAT_SOURCE)
    if status is not None and status >= 400:
        return _result("actionable_http_error", "warning", False,
            "The HTTP error is outside the narrowly recognized expected PAT response and remains visible for diagnosis.", evidence, TROUBLESHOOTING)
    if errors["dns_lookup_failure"] and status is None and evidence["probe_parent_domain"]:
        return _result("expected_dns_probe_failure", "info", True,
            "Name lookup failure on this strict probe subdomain is expected during Turnstile execution and is not itself a fatal challenge error.", evidence, DNS_SOURCE)
    if errors["error_present"]:
        return _result("actionable_dns_failure" if errors["dns_lookup_failure"] else "actionable_request_failure", "warning", False,
            "This observed failure is not covered by an expected probe exception; retain it for diagnosis.", evidence, TROUBLESHOOTING)
    return _result("network_observation", "info", False,
        "No recognized expected failure or actionable request error is present in this observation.", evidence, TROUBLESHOOTING)


def classify_runtime_diagnostic(*, url: str | None = None, page_text: str | None = None,
                                navigator_webdriver: bool | None = None) -> dict:
    """Distinguish an explicit official debug finding from an automation hint.

    Pass visible page text with heading boundaries intact. A quoted/negated
    mention on a different page does not confirm an official debug finding.
    """
    parts = _url_parts(url)
    visible = str(page_text or "")[:100_000]
    finding = bool(re.search(r"(?im)^\s*(?:(?:browser )?status:\s*)?automated browser detected[.!]?\s*$", visible))
    debug = bool(parts and parts["origin"] == DEBUG_SOURCE.rstrip("/") and parts["path"] in ("", "/"))
    evidence = _evidence(url, parts)
    if debug:
        evidence["url_origin"] = DEBUG_SOURCE.rstrip("/")
    evidence.update(explicit_automation_message=finding, official_debug_page=debug,
                    navigator_webdriver=navigator_webdriver if type(navigator_webdriver) is bool else None,
                    page_text_sha256=hashlib.sha256(visible.encode()).hexdigest())
    if finding and debug:
        return _result("debug_automation_detected", "warning", False,
            "The official compatibility page explicitly reported an automated browser. This is a diagnostic-page finding, not confirmation of a publisher's blocking reason.", evidence, DEBUG_SOURCE)
    if finding:
        return _result("unverified_automation_message", "warning", False,
            "Automation-related text was observed without verified official debug-page context; it does not establish a publisher's decision.", evidence, TROUBLESHOOTING)
    if navigator_webdriver is True:
        return _result("automation_signal_observed", "warning", False,
            "navigator.webdriver is true. This is an automation signal, not an explicit compatibility result or a confirmed publisher block.", evidence, TROUBLESHOOTING)
    return _result("runtime_support_unconfirmed", "info", False,
        "These observations do not confirm browser compatibility or a publisher blocking reason.", evidence, TROUBLESHOOTING)
