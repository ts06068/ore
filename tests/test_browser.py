import asyncio
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from ore.browser import BrowserManager
from ore.config import SecretStore, Settings
from ore.policy import AccessDenied, RateLimiter
from ore.store import Store


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/download'):
            content = b'name,value\nalpha,7\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="table.csv"')
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            if self.path.startswith('/challenge-delayed'):
                content = b"""<html><title>Ordinary checkbox fixture</title><body>
                <div data-ore-challenge="true"><label><input id="checkbox" type="checkbox" onclick="setTimeout(() => {document.querySelector('[data-ore-challenge]').remove(); document.querySelector('main').hidden=false;}, 650)">Continue to the controlled article</label></div>
                <main hidden>The requested fixture article has now loaded with substantive content. This controlled asynchronous transition represents an ordinary checkbox response, without asserting access to any external site.</main></body></html>"""
            elif self.path.startswith('/challenge-wrong-page'):
                content = b"""<html><body><div data-ore-challenge="true"><button id="checkbox" onclick="setTimeout(() => {location.href='/wrong-target';}, 150)">Continue</button></div></body></html>"""
            elif self.path.startswith('/wrong-target'):
                content = b"""<html><body><main>This unrelated page contains plenty of readable text, but it is not the requested article. Merely leaving the challenge and reaching this other path must never prove that the requested target became accessible.</main></body></html>"""
            elif self.path.startswith('/challenge'):
                content = b'''<html><title>Challenge fixture</title><body><div data-ore-challenge="true">Ordinary retry test</div>
                <button id="retry">Retry ordinary access</button><button id="solve" onclick="document.querySelector('[data-ore-challenge]').remove();document.querySelector('main').hidden=false">Human completion fixture</button>
                <main hidden>This is the independently observed target content with enough substantive text to confirm the expected page appeared after the fixture interaction.</main></body></html>'''
            else:
                content = b'''<html><title>Archive fixture</title><body><main>A small controlled archive used to test browser ownership, server-side downloads and recorded source observations.</main><a href="/download">Download attachment</a><input type="password"></body></html>'''
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


@pytest.fixture
def site():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def browser_dependencies(monkeypatch):
    local = Path(__file__).resolve().parents[1] / '.ore'
    libraries = local / 'browser-libs/usr/lib/x86_64-linux-gnu'
    if libraries.exists():
        monkeypatch.setenv('LD_LIBRARY_PATH', str(libraries) + (os.pathsep + os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else ''))
    browsers = local / 'browsers'
    if browsers.exists():
        monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(browsers))


def make_manager(tmp_path, site):
    settings = Settings(state_dir=tmp_path / 'state', max_workers=4).prepare()
    store = Store(settings.database_url)
    store.initialize()
    mission = {'goal': 'Exercise browser', 'allowed_origins': [site], 'limits': {'origin_min_interval_seconds': 0}}
    job = store.create_job(mission)
    secrets = SecretStore(settings.state_dir)
    manager = BrowserManager(settings, RateLimiter(), store=store, secrets=secrets)
    return manager, store, secrets, job, mission


@pytest.mark.browser
@pytest.mark.asyncio
async def test_real_browser_handoff_encrypted_profile_and_epoch(tmp_path, site, browser_dependencies):
    manager, store, secrets, job, mission = make_manager(tmp_path, site)
    profile = {'id': '../../untrusted-name', 'allow_private_network': True, 'persist_session': True}
    try:
        session = await manager.create(job['id'], mission, profile, agent_id='task-one')
        await manager.action(session.id, 'navigate', {'url': site})
        observations = store.observations(job['id'])
        assert any(item.get('source_url') == site + '/' for item in observations)
        await session.context.add_cookies([{'name': 'session', 'value': 'fixture-private-cookie', 'url': site}])
        old_epoch = session.epoch
        human = await manager.takeover(session.id)
        with pytest.raises(AccessDenied):
            await manager.observe(session.id)
        with pytest.raises(AccessDenied):
            await manager.action(session.id, 'wait', {'epoch': old_epoch, 'seconds': 0})
        resumed = await manager.resume(session.id)
        assert resumed['epoch'] > human['epoch'] > old_epoch
        with pytest.raises(AccessDenied, match='epoch'):
            await manager.action(session.id, 'wait', {'epoch': old_epoch, 'seconds': 0})
        await manager.action(session.id, 'wait', {'epoch': resumed['epoch'], 'seconds': 0, 'screenshot': False})
        assert 'fixture-private-cookie' not in secrets.path.read_text()
        assert not list((manager.settings.state_dir / 'profiles').glob('*.json'))
        assert all('../' not in name for name in secrets.names())
        await manager.close_session(session.id)
        replacement = await manager.create(job['id'], mission, profile)
        assert any(cookie['value'] == 'fixture-private-cookie' for cookie in await replacement.context.cookies())
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_browser_download_provenance_and_frame_stream(tmp_path, site, browser_dependencies):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        queue = asyncio.Queue(maxsize=1)
        session.subscribers.add(queue)
        await manager.action(session.id, 'navigate', {'url': site})
        frame = await asyncio.wait_for(queue.get(), 10)
        assert frame['type'] == 'frame' and frame['data']
        await manager.action(session.id, 'click', {'epoch': session.epoch, 'selector': 'a', 'screenshot': False})
        for _ in range(50):
            if session.downloads:
                break
            await asyncio.sleep(0.05)
        assert session.downloads[0]['status'] == 'staged'
        assert Path(session.downloads[0]['path']).read_bytes() == b'name,value\nalpha,7\n'
        assert any(item['kind'] == 'browser.download' for item in store.observations(job['id']))
        before = session.epoch
        await manager.takeover(session.id)
        frame = await asyncio.wait_for(queue.get(), 5)
        assert frame['epoch'] > before and frame['control'] == 'human'
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_challenge_attempts_gate_actions_and_human_resolution(tmp_path, site, browser_dependencies):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    mission['on_challenge'] = {'recovery_settle_seconds': 0.1}
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        result = await manager.action(session.id, 'navigate', {'url': site + '/challenge'})
        assert result['challenge_detected']
        with pytest.raises(AccessDenied, match='Challenge'):
            await manager.action(session.id, 'click', {'epoch': session.epoch, 'selector': '#retry'})
        for _ in range(3):
            assert (await manager.challenge(session.id))['allowed']
            await manager.action(session.id, 'click', {'epoch': session.epoch, 'selector': '#retry', 'screenshot': False})
        assert session.control == 'human'
        assert store.get_challenge(session.challenge_id)['state'] == 'awaiting_user'
        await manager.action(session.id, 'click', {'epoch': session.epoch, 'selector': '#solve'}, owner='human')
        result = await manager.verify_challenge(session.id)
        assert result['resolved']
        assert store.get_challenge(session.challenge_id)['state'] == 'resolved'
        await manager.resume(session.id)
        assert session.control == 'agent'
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_screenshot_timeout_preserves_dom_without_unmasked_fallback(tmp_path, site, browser_dependencies, monkeypatch):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        await manager.action(session.id, 'navigate', {'url': site, 'screenshot': False})
        async def failed_capture(**kwargs):
            assert kwargs.get('mask')
            raise TimeoutError('font loading fixture')
        monkeypatch.setattr(session.page, 'screenshot', failed_capture)
        result = await manager.observe(session.id)
        assert 'controlled archive' in result['text']
        assert result['needs_screenshot']
        assert result['screenshot_error']['code'] == 'TimeoutError'
        assert 'image_url' not in result
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_remote_only_coordinator_can_create_public_browser(tmp_path, site, browser_dependencies):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    manager.settings.max_workers = 0
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        assert session.id
        assert (await manager.action(session.id, 'navigate', {'url': site, 'screenshot': False}))['title'] == 'Archive fixture'
    finally:
        await manager.close()
        store.close()


@pytest.mark.asyncio
async def test_close_cleans_all_transports_and_is_idempotent(tmp_path):
    settings = Settings(state_dir=tmp_path / 'state', max_workers=0).prepare()
    manager = BrowserManager(settings, RateLimiter())
    calls = []
    class ClosedBrowser:
        async def close(self):
            calls.append('browser')
            raise RuntimeError('WriteUnixTransport closed')
    class Driver:
        async def stop(self):
            calls.append('driver')
    manager.browser, manager.playwright = ClosedBrowser(), Driver()
    await manager.close()
    await manager.close()
    assert calls == ['browser', 'driver']
    assert manager.browser is None and manager.playwright is None


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize('navigation_error', [False, True])
async def test_reserved_checkbox_waits_for_delayed_target_without_handoff(tmp_path, site, browser_dependencies, monkeypatch, navigation_error):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    mission['on_challenge'] = {'max_attempts_per_episode': 1, 'max_active_seconds': 4}
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        initial = await manager.action(session.id, 'navigate', {'url': site + '/challenge-delayed'})
        assert initial['challenge_detected'] and initial['control'] == 'agent'
        assert initial['image_url'].startswith('data:image/png;base64,')
        reservation = await manager.challenge(session.id)
        assert reservation['allowed'] and reservation['attempts'] == 1 and reservation['control'] == 'agent'
        assert 'token' not in reservation
        target_recovered = manager._target_recovered
        calls = 0
        async def navigation_race(current):
            nonlocal calls
            calls += 1
            if navigation_error and calls <= 2:
                raise PlaywrightError('Execution context was destroyed during fixture navigation')
            return await target_recovered(current)
        monkeypatch.setattr(manager, '_target_recovered', navigation_race)
        result = await manager.action(session.id, 'click', {'epoch': session.epoch, 'selector': '#checkbox', 'screenshot': False})
        record = store.get_challenge(session.challenge_id)
        assert result['control'] == 'agent' and not result['challenge_detected']
        assert record['state'] == 'resolved' and record['attempts'] == 1 and record['token'] is None
        assert 0.5 <= record['active_seconds'] < 4
        assert session.challenge_reservation is None
        assert not any(event['type'] == 'browser_handoff' for event in store.events(job['id']))
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/challenge', '/challenge-wrong-page'])
async def test_failed_recovery_stays_bounded_and_never_accepts_other_page(tmp_path, site, browser_dependencies, path):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    mission['on_challenge'] = {'max_attempts_per_episode': 1, 'max_active_seconds': 0.8, 'recovery_settle_seconds': 100}
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        await manager.action(session.id, 'navigate', {'url': site + path, 'screenshot': False})
        await manager.challenge(session.id)
        started = time.monotonic()
        result = await manager.action(session.id, 'click', {'epoch': session.epoch,
            'selector': '#retry' if path == '/challenge' else '#checkbox', 'screenshot': False})
        record = store.get_challenge(session.challenge_id)
        assert time.monotonic() - started < 3
        assert result['control'] == 'human' and record['state'] == 'awaiting_user'
        assert record['attempts'] == 1 and record['token'] is None and record['active_seconds'] >= 0.7
        assert session.challenge_reservation is None
        verified = await manager.verify_challenge(session.id)
        assert not verified['resolved']
        if path.endswith('wrong-page'):
            assert session.page.url == site + '/wrong-target'
            assert verified['evidence']['reason'] == 'different_page_requires_a_configured_success_check'
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_challenge_reservation_is_idempotent_and_contention_is_not_handoff(tmp_path, site, browser_dependencies):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    mission['on_challenge'] = {'max_attempts_per_episode': 1, 'max_active_seconds': 5, 'recovery_settle_seconds': 0.1}
    try:
        profile = {'id': 'public', 'allow_private_network': True}
        first = await manager.create(job['id'], mission, profile, agent_id='first-agent')
        second = await manager.create(job['id'], mission, profile, agent_id='second-agent')
        for session in (first, second):
            await manager.action(session.id, 'navigate', {'url': site + '/challenge', 'screenshot': False})
        reserved = await manager.challenge(first.id)
        token = first.challenge_reservation['token']
        duplicate = await manager.challenge(first.id)
        assert duplicate['allowed'] and duplicate['reason'] == 'attempt_already_reserved'
        assert duplicate['attempts'] == reserved['attempts'] == 1
        assert first.challenge_reservation['token'] == token
        competing = await manager.challenge(second.id)
        assert not competing['allowed'] and competing['reason'] == 'attempt_in_flight'
        assert competing['code'] == 'challenge_contention' and competing['retryable'] and competing['retry_after'] > 0
        assert competing['control'] == first.control == second.control == 'agent'
        assert second.challenge_reservation is None and 'token' not in competing and 'token' not in duplicate
        assert store.get_challenge(reserved['id'])['token'] == token
        await manager.action(first.id, 'click', {'epoch': first.epoch, 'selector': '#retry', 'screenshot': False})
        exhausted = await manager.challenge(second.id)
        assert not exhausted['allowed'] and exhausted['reason'] == 'budget_exhausted'
        assert exhausted['control'] == 'human' and exhausted['session_id'] == second.id
        assert exhausted['attempts'] == 1
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_expired_local_reservation_does_not_reset_shared_budget(tmp_path, site, browser_dependencies):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    mission['on_challenge'] = {'max_attempts_per_episode': 1, 'max_active_seconds': 0.15}
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        await manager.action(session.id, 'navigate', {'url': site + '/challenge', 'screenshot': False})
        reserved = await manager.challenge(session.id)
        await asyncio.sleep(0.2)
        expired = await manager.challenge(session.id)
        assert expired['reason'] == 'budget_exhausted' and expired['attempts'] == reserved['attempts'] == 1
        assert expired['control'] == 'human' and session.challenge_reservation is None
        record = store.get_challenge(reserved['id'])
        assert record['state'] == 'awaiting_user' and record['token'] is None
        assert record['active_seconds'] >= record['max_active_seconds']
    finally:
        await manager.close()
        store.close()
