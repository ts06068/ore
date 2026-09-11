"""Runtime diagnostic persistence and presentation context without a browser."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore.browser_diagnostics import classify_runtime_diagnostic
from ore.handoffs import HandoffService, create_handoff_router
from ore.store import Store


@pytest.fixture
def handoff_store(tmp_path):
    store=Store("sqlite:///"+str(tmp_path/"store.db"))
    store.initialize()
    job=store.create_job({"goal":"Fixture handoff diagnostic", "access_profile_ref":"public"})
    yield store,job,HandoffService(store)
    store.close()


def diagnostic(explicit=False):
    return classify_runtime_diagnostic(url="https://debug.challenges.cloudflare.com/" if explicit else "https://journal.test/",
        page_text="Automated Browser Detected" if explicit else None,navigator_webdriver=True)


def test_handoff_event_persists_safe_diagnostic_after_session_loss_and_reload(handoff_store):
    store,job,service=handoff_store
    observed=diagnostic()
    observed.update(cookie="secret-cookie",explanation="secret-page-content")
    observed["evidence"]["headers"]={"Authorization":"secret-token"}
    row=service.record_event(job["id"],"browser_handoff",{"session_id":"session","url":"https://journal.test/p?token=secret-query",
        "browser_environment":observed})
    service.record_event(job["id"],"browser_session_lost",{"session_id":"session"})
    restored=HandoffService(store).public(HandoffService(store).get(row["id"]))
    assert restored["session_state"]=="lost"
    assert restored["browser_environment"]["code"]=="automation_signal_observed"
    assert restored["browser_environment"]["evidence"]["publisher_block_confirmed"] is False
    assert restored["browser_environment"]["evidence"]["url_sha256"]==observed["evidence"]["url_sha256"]
    assert "secret" not in json.dumps(restored)


def test_same_handoff_updates_explicit_debug_evidence_without_inventing_publisher_cause(handoff_store):
    _,job,service=handoff_store
    first=service.create(job["id"],"challenge","Initial",session_id="session",context={"browser_environment":diagnostic()})
    updated=service.create(job["id"],"challenge","Updated",session_id="session",context={"browser_environment":diagnostic(True)})
    assert updated["id"]==first["id"]
    assert updated["browser_environment"]["code"]=="debug_automation_detected"
    assert updated["browser_environment"]["evidence"]["publisher_block_confirmed"] is False
    replay=service.create(job["id"],"challenge","Still waiting",session_id="session")
    assert replay["browser_environment"]==updated["browser_environment"]


def test_bare_or_spoofed_diagnostic_code_does_not_confirm_official_debug_finding(handoff_store):
    _,job,service=handoff_store
    value=diagnostic();value["code"]="debug_automation_detected"
    row=service.create(job["id"],"challenge","Fixture",session_id="session",context={"browser_environment":value})
    assert row["browser_environment"]["code"]=="automation_signal_observed"
    assert not row["browser_environment"]["evidence"]["official_debug_page"]
    plain=service.create(job["id"],"challenge","Ordinary challenge",session_id="other")
    assert "browser_environment" not in plain


@pytest.mark.asyncio
async def test_live_summary_diagnostic_is_durable_and_polling_does_not_churn_version(handoff_store):
    _,job,service=handoff_store
    row=service.create(job["id"],"challenge","Fixture",session_id="session")
    browser=SimpleNamespace(exists=lambda sid:True,summary=AsyncMock(return_value={"epoch":4,"url":"https://journal.test/","browser_environment":diagnostic()}))
    router=create_handoff_router(SimpleNamespace(handoffs=service),browser)
    output=await router.handoff_output(row)
    assert output["browser_environment"]["code"]=="automation_signal_observed"
    assert service.get(row["id"])["browser_environment"]==output["browser_environment"]
    repeated=await router.handoff_output(service.get(row["id"]))
    assert repeated["state_version"]==output["state_version"]

@pytest.mark.asyncio
async def test_live_transport_replaces_stale_desktop_label_and_environment(handoff_store):
    _, job, service = handoff_store
    row = service.create(job['id'], 'challenge', 'Fixture', session_id='session')
    row = service.update(row['id'], {'browser_transport': 'desktop_chrome', 'browser_environment': diagnostic()})
    browser = SimpleNamespace(exists=lambda sid: True, summary=AsyncMock(return_value={
        'epoch': 5, 'url': 'https://journal.test/', 'transport': 'playwright', 'browser_environment': None}))
    router = create_handoff_router(SimpleNamespace(handoffs=service), browser)
    output = await router.handoff_output(row)
    assert output['browser_transport'] == 'playwright'
    assert output['browser_environment'] is None
    repeated = await router.handoff_output(service.get(row['id']))
    assert repeated['state_version'] == output['state_version']
