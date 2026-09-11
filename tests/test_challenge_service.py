from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio

from ore.browser import BrowserManager
from ore.challenge_service import ChallengeCoordinator


async def test_remote_deadline_handoff_retains_trusted_task_ownership():
    episode={'id':'challenge','clock':'elapsed','state':'attempting','deadline_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}
    store=SimpleNamespace(list_documents=lambda *a,**k:[episode],adapt_challenge=Mock())
    remote={'id':'session','job_id':'job','assignment_id':'assignment','challenge_id':'challenge','control':'agent','task_id':'untrusted-summary-value'}
    execution=SimpleNamespace(enabled=True,list_sessions=AsyncMock(return_value=[remote]),
        _get=lambda kind,ident:{'task':{'id':'trusted-task'}},browser_command=AsyncMock())
    engine=SimpleNamespace(store=store,browser=SimpleNamespace(sessions={}),execution=execution,handoffs=SimpleNamespace(create=Mock()))
    service=ChallengeCoordinator(engine)
    try:
        await service.tick();await asyncio.gather(*service.pending.values())
        assert engine.handoffs.create.call_args.kwargs['task_id']=='trusted-task'
        execution.browser_command.assert_awaited_once_with('session','takeover')
    finally:await service.close()


async def test_native_failure_fingerprint_uses_actual_latest_observation():
    manager=object.__new__(BrowserManager)
    session=SimpleNamespace(context=SimpleNamespace(desktop=True),page=SimpleNamespace(url='https://fixture.test/issue',latest={'text':'Verification pending alpha'}),last_status=None,browser_environment={})
    first=await manager._challenge_evidence(session)
    session.page.latest={'text':'Verification accepted beta'}
    second=await manager._challenge_evidence(session)
    assert first['fingerprint']!=second['fingerprint']
    assert second['verification_accepted'] is False  # Text alone is not a checked control receipt.
