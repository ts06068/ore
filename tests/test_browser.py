import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

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
            if self.path.startswith('/challenge'):
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
