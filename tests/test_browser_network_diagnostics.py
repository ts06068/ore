"""Network failures retain their meaning without broadening browser access."""
import asyncio
import json
import pytest
from ore.policy import DNSResolutionFailed
from test_browser import browser_dependencies, make_manager, site

@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize('target,configured,expected', [
    ('https://private-probe.challenges.cloudflare.com/private-path?token=fixture-secret&session=fixture-session', True, True),
    ('https://challenges.cloudflare.com/turnstile/api.js', True, False),
    ('https://probe.challenges.cloudflare.com/probe', False, False),
])
async def test_browser_preserves_dns_semantics_and_scope(tmp_path, site, browser_dependencies, target, configured, expected):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    if configured:
        from urllib.parse import urlsplit
        u=urlsplit(target)
        mission['scope']={'browser_support_origins':[f'{u.scheme}://{u.netloc}']}
    try:
        session = await manager.create(job['id'], mission, {'id':'public','allow_private_network':True})
        await manager.action(session.id, 'navigate', {'url':site,'screenshot':False})
        original = session.policy.check_browser_request
        async def fail_after_admission(url, **kwargs):
            await original(url, **kwargs)
            if url == target:
                raise DNSResolutionFailed('DNS resolution failed')
        session.policy.check_browser_request = fail_after_admission
        await session.page.evaluate('url => fetch(url).catch(() => null)',target)
        for _ in range(50):
            if session.network_failures:break
            await asyncio.sleep(.02)
        observation=session.network_failures[-1]
        assert observation['diagnostic']['expected'] is expected
        for private in ('private-probe', 'private-path', 'fixture-secret', 'fixture-session'):
            assert private not in json.dumps(observation)
        if configured:
            assert observation['error_code'] == 'net::ERR_NAME_NOT_RESOLVED'
        else:
            # Chromium may append the CDP routing source as '.Inspector'.
            assert observation['error_code'].split('.', 1)[0] == 'net::ERR_BLOCKED_BY_CLIENT'
        summary=await manager.summary(session)
        assert summary['network_diagnostics']['count'] == (0 if expected else 1)
        if not configured:
            assert summary['network_diagnostics']['blocked_requests'][0]['reason'].startswith('Origin outside')
        assert not store.get_challenge('nonexistent')
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_http_diagnostics_omit_private_path_query_and_arbitrary_header(tmp_path, site, browser_dependencies, monkeypatch):
    from test_browser import Handler
    original_get = Handler.do_GET
    def fixture_response(handler):
        if handler.path.startswith('/cdn-cgi/challenge-platform/'):
            handler.send_response(401)
            handler.send_header('Content-Length', '0')
            handler.send_header('cf-mitigated', 'fixture-private-header')
            handler.end_headers()
        else:
            original_get(handler)
    monkeypatch.setattr(Handler, 'do_GET', fixture_response)
    manager, store, _, job, mission = make_manager(tmp_path, site)
    manager.on_event = store.append_event
    try:
        session = await manager.create(job['id'], mission, {'id':'public','allow_private_network':True})
        await manager.action(session.id, 'navigate', {'url':site,'screenshot':False})
        target = site + '/cdn-cgi/challenge-platform/h/g/pat/fixture-private-path?token=fixture-secret&session=fixture-session'
        await session.page.evaluate('url => fetch(url).then(r => r.status)', target)
        for _ in range(50):
            if session.response_diagnostics and any(e['type'] == 'browser.http_status' for e in store.events(job['id'])):break
            await asyncio.sleep(.02)
        observation = session.response_diagnostics[-1]
        assert observation['status'] == 401 and observation['url_origin'] == site
        assert len(observation['url_sha256']) == 64 and observation['cf_mitigated'] is None
        events = [event for event in store.events(job['id']) if event['type'] == 'browser.http_status']
        assert events
        for record in [observation, *events]:
            serialized = json.dumps(record)
            for private in ('fixture-private-path', 'fixture-secret', 'fixture-session', 'fixture-private-header'):
                assert private not in serialized
    finally:
        await manager.close()
        store.close()


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize('dns_failure', [False, True])
async def test_diagnostic_sink_failure_cannot_stall_denial_and_close_drains_tasks(
        tmp_path, site, browser_dependencies, monkeypatch, dns_failure):
    from test_browser import Handler
    original_get = Handler.do_GET
    def fixture_response(handler):
        if handler.path == '/callback-status':
            handler.send_response(401)
            handler.send_header('Content-Length', '0')
            handler.end_headers()
        else:
            original_get(handler)
    monkeypatch.setattr(Handler, 'do_GET', fixture_response)
    manager, store, _, job, mission = make_manager(tmp_path, site)
    target = 'https://challenges.cloudflare.com/turnstile/api.js' if dns_failure else 'https://outside.example/blocked'
    if dns_failure:
        mission['scope'] = {'browser_support_origins': ['https://challenges.cloudflare.com']}
    entered, cancelled, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def broken_sink(job_id, kind, payload):
        if kind in ('request_blocked', 'browser.network_failure'):
            raise RuntimeError('fixture-private-sink-token')
        if kind == 'browser.http_status':
            entered.set()
            try:
                await never.wait()
            finally:
                cancelled.set()
    manager.on_event = broken_sink
    loop = asyncio.get_running_loop()
    previous_handler, unhandled = loop.get_exception_handler(), []
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        await manager.action(session.id, 'navigate', {'url': site, 'screenshot': False})
        original_check = session.policy.check_browser_request
        async def injected_dns_failure(url, **kwargs):
            await original_check(url, **kwargs)
            if dns_failure and url == target:
                raise DNSResolutionFailed('fixture-private-dns-token')
        session.policy.check_browser_request = injected_dns_failure
        # A failing request_blocked sink must still abort the denied request.
        result = await asyncio.wait_for(session.page.evaluate(
            'url => fetch(url).then(() => "allowed").catch(() => "blocked")', target), 3)
        assert result == 'blocked'
        for _ in range(100):
            if len(session.observer_errors) >= 2:
                break
            await asyncio.sleep(.01)
        errors = session.observer_errors
        assert {error['kind'] for error in errors} >= {'request_blocked', 'requestfailed'}
        assert session.network_failures[-1]['error_code'] == (
            'net::ERR_NAME_NOT_RESOLVED' if dns_failure else 'net::ERR_BLOCKED_BY_CLIENT')
        assert len(session.request_denials) == 1
        assert 'fixture-private' not in json.dumps((await manager.summary(session))['network_diagnostics'])
        # An in-flight diagnostic sink is cancelled and retrieved on close.
        await session.page.evaluate('url => fetch(url).then(r => r.status)', site + '/callback-status')
        await asyncio.wait_for(entered.wait(), 3)
        pending = list(session.observers)
        assert any(not task.done() for task in pending)
        await asyncio.wait_for(manager.close_session(session.id), 3)
        assert session.closed and not session.observers
        assert cancelled.is_set() and all(task.done() for task in pending)
        await asyncio.sleep(.05)
        assert not unhandled
    finally:
        await manager.close()
        loop.set_exception_handler(previous_handler)
        store.close()
