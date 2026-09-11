"""Exercise the real ORE control/ledger boundary with a fake native desktop only."""
import asyncio
from types import SimpleNamespace
import pytest
from ore.browser import BrowserManager
from ore.config import Settings
from ore.engine import Engine
from ore.policy import AccessDenied, RateLimiter
from ore.store import Store
from ore.tools import ToolRuntime
from test_desktop_browser import NativeFixture

class InteractiveDesktop(NativeFixture):
    async def action(self, sid, action, args):
        await super().action(sid, action, args)
        if action == 'navigate':
            self.value.update(title='Just a moment...', text='Verify you are human')
        if action == 'click':
            self.value.update(title='Independent target', text='Independent target article now visible with substantive study results.')
    async def close(self):
        pass

@pytest.mark.asyncio
async def test_native_manager_uses_shared_challenge_and_control_ledger(tmp_path, monkeypatch):
    runtime = InteractiveDesktop()
    monkeypatch.setattr('ore.desktop.DesktopRuntime', lambda *a, **k: runtime)
    async def forbidden_playwright(*args, **kwargs):
        pytest.fail('Native backend started Playwright')
    monkeypatch.setattr(BrowserManager, 'start', forbidden_playwright)
    settings = Settings(state_dir=tmp_path, browser_backend='desktop_chrome').prepare()
    store = Store(settings.database_url); store.initialize()
    manager = BrowserManager(settings, RateLimiter(), store=store)
    mission = {'goal': 'Native control fixture', 'allowed_origins': ['https://journal.test'],
        'limits': {'origin_min_interval_seconds': 0},
        'on_challenge': {'max_attempts_per_episode': 1, 'max_active_seconds': 30, 'recovery_settle_seconds': 1}}
    profile = {'id': 'desktop-test', 'allow_private_network': True,
        'desktop_success_text': ['Independent target article']}
    job = store.create_job(mission)
    try:
        session = await manager.create(job['id'], mission, profile)
        first = await manager.action(session.id, 'navigate', {'url': 'https://journal.test/article'})
        assert first['transport'] == 'desktop_chrome' and first['challenge_detected']
        assert first['http_status'] is None and first['elements'] == []
        with pytest.raises(AccessDenied, match='reserve a bounded attempt'):
            await manager.action(session.id, 'click', {'epoch': first['epoch'], 'x': 200, 'y': 250})
        reserved = await manager.challenge(session.id)
        assert reserved['allowed'] and reserved['attempts'] == 1
        recovered = await manager.action(session.id, 'click', {'epoch': first['epoch'], 'x': 200, 'y': 250})
        assert recovered['challenge_detected'] is False
        assert store.get_challenge(session.challenge_id)['state'] == 'resolved'
        assert recovered['http_status'] is None
        human = await manager.takeover(session.id)
        with pytest.raises(AccessDenied, match='control belongs'):
            await manager.action(session.id, 'click', {'epoch': human['epoch'], 'x': 200, 'y': 250})
        with pytest.raises(AccessDenied, match='Stale'):
            await manager.action(session.id, 'click', {'epoch': first['epoch'], 'x': 200, 'y': 250}, owner='human')
        await manager.action(session.id, 'click', {'epoch': human['epoch'], 'x': 200, 'y': 250}, owner='human')
        assert runtime.created and manager.playwright is None
    finally:
        await manager.close(); store.close()

@pytest.mark.asyncio
async def test_systematic_native_observation_cannot_be_minted_as_html(tmp_path):
    engine = Engine(Settings(state_dir=tmp_path, max_workers=0))
    try:
        job = engine.create({'goal': 'Read only screenshot', 'artifact_roles': []})
        runtime = ToolRuntime(engine, job['id'])
        class NoDOM:
            def locator(self, *a):
                pytest.fail('OCR observation was queried as DOM')
        session = SimpleNamespace(context=SimpleNamespace(desktop=True), page=NoDOM())
        engine.browser.sessions['fixture'] = session
        # Supply the actual lookup contract without creating a live desktop.
        engine.browser.get = lambda sid: session
        value = {'session_id': 'fixture', 'control': 'agent'}
        await runtime._capture_browser(value, {'completeness': 'systematic'})
        assert value['coverage_capture']['captured'] is False
        assert value['coverage_capture']['completeness_verified'] is False
        assert 'snapshot_id' not in value
        engine.browser.sessions.clear()
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('reserve_first', [False, True])
async def test_native_passive_wait_preserves_challenge_accounting_and_click_gate(tmp_path, monkeypatch, reserve_first):
    runtime = InteractiveDesktop()
    monkeypatch.setattr('ore.desktop.DesktopRuntime', lambda *args, **kwargs: runtime)
    settings = Settings(state_dir=tmp_path, browser_backend='desktop_chrome').prepare()
    store = Store(settings.database_url)
    store.initialize()
    manager = BrowserManager(settings, RateLimiter(), store=store)
    mission = {'goal': 'Wait for native verification', 'allowed_origins': ['https://journal.test'],
        'limits': {'origin_min_interval_seconds': 0},
        'on_challenge': {'max_attempts_per_episode': 1, 'max_active_seconds': 30, 'recovery_settle_seconds': 1}}
    profile = {'id': 'passive-wait-fixture', 'allow_private_network': True,
        'desktop_success_text': ['Independent target article']}
    job = store.create_job(mission)
    settle_calls = []
    original_settle = manager._settle_challenge_recovery
    async def record_settle(session, reservation):
        settle_calls.append(reservation['id'])
        return await original_settle(session, reservation)
    monkeypatch.setattr(manager, '_settle_challenge_recovery', record_settle)
    try:
        session = await manager.create(job['id'], mission, profile)
        first = await manager.action(session.id, 'navigate', {'url': 'https://journal.test/article'})
        assert first['challenge_detected']
        before = None
        if reserve_first:
            reserved = await manager.challenge(session.id)
            assert reserved['allowed'] and reserved['attempts'] == 1
            before = store.get_challenge(session.challenge_id)
        waited = await manager.action(session.id, 'wait', {'epoch': session.epoch, 'seconds': .01})
        assert waited['challenge_detected'] and waited['control'] == 'agent'
        assert settle_calls == []
        assert [action for _, action, _ in runtime.actions] == ['navigate']
        assert not any(event['type'] == 'browser_handoff' for event in store.events(job['id']))
        if reserve_first:
            after = store.get_challenge(session.challenge_id)
            for key in ('token', 'attempts', 'active_seconds', 'expires_at', 'state'):
                assert after[key] == before[key]
            assert session.challenge_reservation['token'] == before['token']
        else:
            assert session.challenge_reservation is None and session.challenge_id is None
            assert store.list_documents('challenge', job_id=job['id']) == []
            with pytest.raises(AccessDenied, match='reserve a bounded attempt'):
                await manager.action(session.id, 'click', {'epoch': session.epoch, 'x': 200, 'y': 250})
            assert [action for _, action, _ in runtime.actions] == ['navigate']
            await manager.challenge(session.id)
        result = await manager.action(session.id, 'click', {'epoch': session.epoch, 'x': 200, 'y': 250})
        completed = store.get_challenge(session.challenge_id)
        assert result['challenge_detected'] is False and result['control'] == 'agent'
        assert completed['state'] == 'resolved' and completed['attempts'] == 1
        assert completed['token'] is None and session.challenge_reservation is None
        assert settle_calls == [session.challenge_id]
    finally:
        await manager.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', ['access', 'desktop'])
async def test_native_observation_failure_finishes_attempt_without_claiming_recovery(tmp_path, monkeypatch, error_type):
    from ore.desktop import DesktopError
    runtime = InteractiveDesktop()
    monkeypatch.setattr('ore.desktop.DesktopRuntime', lambda *args, **kwargs: runtime)
    settings = Settings(state_dir=tmp_path, browser_backend='desktop_chrome').prepare()
    store = Store(settings.database_url)
    store.initialize()
    manager = BrowserManager(settings, RateLimiter(), store=store)
    mission = {'goal': 'Native failure fixture', 'allowed_origins': ['https://journal.test'],
        'limits': {'origin_min_interval_seconds': 0},
        'on_challenge': {'max_attempts_per_episode': 1, 'max_active_seconds': 30, 'recovery_settle_seconds': .03}}
    job = store.create_job(mission)
    try:
        session = await manager.create(job['id'], mission, {'id': 'failure-fixture', 'allow_private_network': True})
        await manager.action(session.id, 'navigate', {'url': 'https://journal.test/article'})
        reserved = await manager.challenge(session.id)
        async def unavailable(_session):
            raise (AccessDenied if error_type == 'access' else DesktopError)('fixture-private-observation-error')
        monkeypatch.setattr(manager, '_target_recovered', unavailable)
        result = await asyncio.wait_for(manager.action(session.id, 'click',
            {'epoch': session.epoch, 'x': 200, 'y': 250}), 2)
        record = store.get_challenge(session.challenge_id)
        assert reserved['attempts'] == record['attempts'] == 1
        assert record['state'] == 'awaiting_user' and record['active_seconds'] > 0
        assert record['token'] is None and record['evidence'] is None
        assert session.challenge_reservation is None and result['control'] == 'human'
        assert any(event['type'] == 'challenge.finished' for event in store.events(job['id']))
        assert 'fixture-private-observation-error' not in str(store.events(job['id']))
    finally:
        await manager.close()
        store.close()
