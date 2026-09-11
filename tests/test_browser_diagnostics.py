"""Observation-only classifications must not hide unrelated failures or secrets."""
import json

import pytest

from ore.browser_diagnostics import classify_network_observation, classify_runtime_diagnostic


@pytest.mark.parametrize("host", ["brunhild.challenges.cloudflare.com", "a.b.challenges.cloudflare.com", "probe.dnstest.dev", "PROBE.DNSTEST.DEV."])
@pytest.mark.parametrize("error", ["net::ERR_NAME_NOT_RESOLVED", "ENOTFOUND", "Name or service not known"])
def test_strict_probe_name_lookup_failures_are_expected(host, error):
    value = classify_network_observation("https://" + host + "/", error_reason=error)
    assert value["code"] == "expected_dns_probe_failure"
    assert value["expected"] is True
    assert value["severity"] == "info"
    assert value["evidence"]["dns_lookup_failure"] is True


@pytest.mark.parametrize("host", ["challenges.cloudflare.com", "dnstest.dev", "evilchallenges.cloudflare.com", "a.challenges.cloudflare.com.evil.test", "notdnstest.dev", "a..dnstest.dev"])
def test_apex_lookalikes_and_malformed_hosts_remain_actionable(host):
    value = classify_network_observation("https://" + host + "/", error_code="net::ERR_NAME_NOT_RESOLVED")
    assert value["expected"] is False
    assert value["severity"] == "warning"


@pytest.mark.parametrize("error", ["scope_blocked", "URL is not allowed", "net::ERR_TIMED_OUT", "net::ERR_DNS_TIMED_OUT", "net::ERR_ABORTED", "net::ERR_CONNECTION_REFUSED", "net::ERR_FAILED"])
def test_probe_host_does_not_hide_non_lookup_failures(error):
    value = classify_network_observation("https://probe.challenges.cloudflare.com/", error_code=error)
    assert value["expected"] is False
    assert value["severity"] == "warning"


def test_blocked_event_with_dns_context_is_not_downgraded():
    value = classify_network_observation("https://probe.dnstest.dev/", error_code="scope_blocked", error_reason="net::ERR_NAME_NOT_RESOLVED")
    assert value["code"] == "actionable_request_failure"


@pytest.mark.parametrize("url,top", [
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pat/opaque/next", None),
    ("https://journal.example/cdn-cgi/challenge-platform/h/g/pat/opaque", "https://journal.example/article"),
])
def test_pat401_needs_exact_path_and_cloudflare_or_authorized_same_origin(url, top):
    value = classify_network_observation(url, status=401, authorized_top_origin=top)
    assert value["code"] == "expected_pat_unauthorized"
    assert value["expected"] is True
    assert value["evidence"]["http_status"] == 401


@pytest.mark.parametrize("url,top", [
    ("https://challenges.cloudflare.com/api/pat/token", None),
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pattern/token", None),
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pat/", None),
    ("https://challenges.cloudflare.com.evil.test/cdn-cgi/challenge-platform/h/g/pat/token", None),
    ("https://probe.challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pat/token", None),
    ("https://journal.example/cdn-cgi/challenge-platform/h/g/pat/token", None),
    ("https://journal.example:8443/cdn-cgi/challenge-platform/h/g/pat/token", "https://journal.example"),
    ("http://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pat/token", None),
])
def test_ordinary401_is_not_treated_as_pat(url, top):
    value = classify_network_observation(url, status=401, authorized_top_origin=top)
    assert value["code"] == "actionable_http_error"
    assert value["expected"] is False


def test_pat_path_does_not_hide_other_status_or_request_error():
    url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pat/token"
    assert not classify_network_observation(url, status=403)["expected"]
    assert not classify_network_observation(url, status=401, error_code="ERR_TIMED_OUT")["expected"]
    assert not classify_network_observation("https://a.dnstest.dev/", status=403, error_code="ENOTFOUND")["expected"]


def test_observation_evidence_does_not_echo_credentials_opaque_paths_or_bodies():
    value = classify_network_observation("https://user:password@challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pat/private-path?token=secret-query#private-fragment", status=401)
    rendered = json.dumps(value)
    for secret in ("user:", "password", "private-path", "secret-query", "private-fragment"):
        assert secret not in rendered
    value = classify_network_observation("https://journal.example/private-path?token=secret-query", error_reason="unknown error credential=secret-reason")
    assert "secret-reason" not in json.dumps(value)
    assert "private-path" not in json.dumps(value)
    assert "secret-query" not in json.dumps(value)


def test_official_debug_finding_is_distinct_from_webdriver_hint():
    actual = classify_runtime_diagnostic(url="https://debug.challenges.cloudflare.com/", page_text="Compatibility report\nAutomated Browser Detected\nUse a normal browser", navigator_webdriver=True)
    assert actual["code"] == "debug_automation_detected"
    assert actual["evidence"]["publisher_block_confirmed"] is False
    hint = classify_runtime_diagnostic(url="https://journal.example/", navigator_webdriver=True)
    assert hint["code"] == "automation_signal_observed"
    assert hint["evidence"]["explicit_automation_message"] is False


@pytest.mark.parametrize("url", [None, "https://journal.example/", "https://debug.challenges.cloudflare.com.evil.test/", "http://debug.challenges.cloudflare.com/"])
def test_text_without_exact_debug_context_does_not_confirm_debug_result(url):
    value = classify_runtime_diagnostic(url=url, page_text="Automated Browser Detected")
    assert value["code"] == "unverified_automation_message"
    assert value["evidence"]["publisher_block_confirmed"] is False


@pytest.mark.parametrize("text", ["No Automated Browser Detected", "This explains Automated Browser Detected messages", "No errors"])
def test_absent_or_negated_debug_finding_does_not_confirm_compatibility(text):
    value = classify_runtime_diagnostic(url="https://debug.challenges.cloudflare.com/", page_text=text, navigator_webdriver=False)
    assert value["code"] == "runtime_support_unconfirmed"


def test_runtime_diagnostic_does_not_leak_page_or_query():
    value = classify_runtime_diagnostic(url="https://debug.challenges.cloudflare.com/?token=secret-query", page_text="Automated Browser Detected\nprivate-cookie-value")
    assert value["code"] == "debug_automation_detected"
    assert "private-cookie-value" not in json.dumps(value)
    assert "secret-query" not in json.dumps(value)


def test_error_words_only_in_url_do_not_become_observed_dns_codes():
    value = classify_network_observation("https://probe.dnstest.dev/", error_reason="Request failed for https://probe.dnstest.dev/?hint=ERR_NAME_NOT_RESOLVED")
    assert value["expected"] is False
    assert value["evidence"]["dns_lookup_failure"] is False


def test_unnormalized_pat_path_is_not_an_expected401():
    value = classify_network_observation("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/pat/token/../../account", status=401)
    assert value["code"] == "actionable_http_error"
