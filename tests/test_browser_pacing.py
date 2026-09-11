"""Ordinary page loading remains bounded without delaying every asset by seconds."""
import asyncio
from datetime import datetime, timedelta, timezone
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from ore.policy import AccessPolicy, RateLimiter
from ore.store import Store
import ore.store as store_module
from test_browser import browser_dependencies, make_manager


def test_durable_resource_lane_avoids_legacy_backlog_but_shares_429(tmp_path, monkeypatch):
    now = [datetime(2026, 9, 10, tzinfo=timezone.utc)]
    monkeypatch.setattr(store_module, 'utcnow', lambda: now[0])
    store = Store('sqlite:///' + str(tmp_path / 'rate.db')); store.initialize()
    other = Store(store.database_url)
    key = 'profile:publisher'
    try:
        main = [store.acquire_rate_slot(key, 3) for _ in range(3)]
        assert [row['delay_seconds'] for row in main] == [0, 3, 6]
        resource = [other.acquire_rate_slot(key, .1, lane='browser_resource') for _ in range(2)]
        assert [row['delay_seconds'] for row in resource] == [0, .1]
        assert store.get_document('rate', key)['interval'] == 3
        assert store.get_document('rate', key)['next_at'].endswith('00:00:09+00:00')
        other.penalize_rate(key, 10)
        now[0] += timedelta(seconds=2)
        assert store.confirm_rate_slot(key, main[0]['slot_at'])['delay_seconds'] == 8
        assert other.confirm_rate_slot(key, resource[0]['slot_at'], lane='browser_resource')['delay_seconds'] == 8
        now[0] += timedelta(seconds=8)
        assert store.confirm_rate_slot(key, main[0]['slot_at'])['allowed']
        assert other.confirm_rate_slot(key, resource[0]['slot_at'], lane='browser_resource')['allowed']
        assert store.confirm_rate_slot(key, main[1]['slot_at'])['delay_seconds'] == 3
        assert other.confirm_rate_slot(key, resource[1]['slot_at'], lane='browser_resource')['delay_seconds'] == .1
        with pytest.raises(ValueError): store.acquire_rate_slot(key, .1, lane='unbounded')
    finally:
        other.close(); store.close()


@pytest.mark.asyncio
async def test_in_memory_waiters_recheck_shared_429():
    limiter = RateLimiter()
    await limiter.acquire('origin', .02, lane='browser_resource')
    started = time.monotonic()
    pending = asyncio.create_task(limiter.acquire('origin', .02, lane='browser_resource'))
    await asyncio.sleep(.005)
    await limiter.penalize('origin', .15)
    await pending
    assert time.monotonic() - started >= .14


def test_lane_classification_does_not_relax_navigation_api_or_regular_data():
    page = 'https://publisher.test/article'
    support = 'https://challenges.cloudflare.com'
    policy = AccessPolicy({'scope': {'browser_support_origins': [support]}})
    def lane(url, kind, top=False):
        return policy.browser_request_lane(url, top_level_url=page,
            is_top_level_navigation=top, resource_type=kind)
    assert lane(page, 'document', True) == 'main'
    assert lane('https://publisher.test/data', 'fetch') == 'main'
    assert lane('https://publisher.test/download', 'document') == 'main'
    assert lane('https://api.elsevier.com/content/search/scopus', 'script') == 'main'
    assert lane(support + '/widget', 'document') == 'browser_resource'
    assert lane(support + '/response', 'xhr') == 'browser_resource'
    assert lane('https://brunhild.challenges.cloudflare.com/check', 'fetch') == 'main'
    assert lane('https://publisher.test/cdn-cgi/challenge-platform/bootstrap', 'fetch') == 'browser_resource'
    assert lane('https://unrelated.test/cdn-cgi/challenge-platform/bootstrap', 'fetch') == 'main'
    assert lane('https://publisher.test/style.css', 'stylesheet') == 'browser_resource'


@pytest.fixture
def resource_site():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            seen.append((self.path, time.monotonic()))
            status, kind = 200, 'text/javascript'
            if self.path == '/':
                kind = 'text/html'
                body = ("<html><title>Resource fixture</title><body><main>Bounded page loading.</main><script>window.loaded=0</script>" +
                    ''.join(f'<script defer src="/asset-{i}.js"></script>' for i in range(6)) + '</body></html>').encode()
            elif self.path.startswith('/data'):
                kind, body = 'text/plain', self.headers.get('Cookie', '').encode()
            elif self.path == '/limited.js':
                status, body = 429, b'limited'
            else:
                body = b'window.loaded=(window.loaded||0)+1;'
            self.send_response(status)
            if self.path == '/': self.send_header('Set-Cookie', 'fixture_session=kept; Path=/')
            if status == 429: self.send_header('Retry-After', '.3')
            self.send_header('Content-Type', kind); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f'http://127.0.0.1:{server.server_port}', seen
    server.shutdown(); thread.join(); server.server_close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_actual_multi_resource_page_meets_deadline_keeps_data_limit_and_cookies(tmp_path, resource_site, browser_dependencies):
    site, seen = resource_site
    manager, store, _, job, mission = make_manager(tmp_path, site)
    manager.limiter = RateLimiter(store)
    mission['limits'] = {'origin_min_interval_seconds': 3}
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        started = time.monotonic()
        await asyncio.wait_for(manager.action(session.id, 'navigate', {'url': site, 'screenshot': False}), 4)
        assert await session.page.evaluate('window.loaded') == 6
        assert time.monotonic() - started < 3
        assert await session.page.evaluate("fetch('/data?token=synthetic').then(r=>r.text())") == 'fixture_session=kept'
        nav_time = next(at for path, at in seen if path == '/')
        data_time = next(at for path, at in seen if path.startswith('/data'))
        assert data_time - nav_time >= 2.8
        diagnostics = (await manager.summary(session))['network_diagnostics']
        grants = diagnostics['request_grants']
        assert len(grants) <= 100
        assert len([row for row in grants if row['lane'] == 'browser_resource' and '/asset-' in row['url']]) == 6
        data = next(row for row in grants if '/data?' in row['url'])
        assert data['lane'] == 'main' and data['queue_seconds'] > 1
        assert 'synthetic' not in data['url']
    finally:
        await manager.close(); store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_actual_browser_429_holds_resource_and_data_lanes(tmp_path, resource_site, browser_dependencies):
    site, seen = resource_site
    manager, store, _, job, mission = make_manager(tmp_path, site)
    manager.limiter = RateLimiter(store)
    mission['limits'] = {'origin_min_interval_seconds': 0, 'browser_resource_min_interval_seconds': .02}
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        await manager.action(session.id, 'navigate', {'url': site, 'screenshot': False})
        await session.page.evaluate("new Promise(resolve=>{let s=document.createElement('script');s.src='/limited.js';s.onerror=()=>resolve();document.body.appendChild(s)})")
        for _ in range(100):
            rate = store.get_document('rate', 'public:127.0.0.1')
            if rate and rate.get('blocked_until'): break
            await asyncio.sleep(.005)
        assert rate.get('blocked_until')
        await asyncio.gather(session.page.add_script_tag(url=site+'/after429.js'), session.page.evaluate("fetch('/data-after429').then(r=>r.text())"))
        limited = next(at for path, at in seen if path == '/limited.js')
        for name in ('/after429.js', '/data-after429'):
            assert next(at for path, at in seen if path == name) - limited >= .27
    finally:
        await manager.close(); store.close()


@pytest.mark.asyncio
async def test_invalid_resource_interval_cannot_leak_a_new_browser(tmp_path, monkeypatch):
    from ore.policy import AccessDenied
    manager, store, _, job, mission = make_manager(tmp_path, 'https://example.org')
    mission['limits']['browser_resource_min_interval_seconds'] = 0
    async def unexpected_start():
        raise AssertionError('Invalid limits must fail before browser launch')
    monkeypatch.setattr(manager, 'start', unexpected_start)
    try:
        with pytest.raises(AccessDenied, match='positive'):
            await manager.create(job['id'], mission)
        assert not manager.sessions
    finally:
        await manager.close(); store.close()
