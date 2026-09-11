"""Native adapter contracts tested without a live site, desktop, or credentials."""
import asyncio
import hashlib
import struct
import zlib
from types import SimpleNamespace

import pytest

from ore.desktop_browser import create_context, observe, target_recovered
from ore.policy import AccessDenied


def fixture_png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1280, 800, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff' * 1280 * 3) * 800)) + chunk(b'IEND', b''))


class NativeFixture:
    def __init__(self):
        self.created, self.actions, self.closed = [], [], []
        self.observations = 0
        self.value = {'url': 'about:blank', 'url_observed': True, 'title': '', 'text': '',
                      'png': fixture_png(), 'width': 1280, 'height': 800}

    async def create_session(self, sid, *, allowed_origins, task_budget_seconds=None):
        self.created.append((sid, allowed_origins))
        return SimpleNamespace(id=sid)

    async def observe(self, sid):
        self.observations += 1
        return dict(self.value)

    async def screenshot(self, sid):
        return self.value['png']

    async def action(self, sid, action, args):
        self.actions.append((sid, action, args))
        if action == 'navigate':
            self.value['url'] = args['url']

    async def close_session(self, sid):
        self.closed.append(sid)


async def fixture_session(mission=None, profile=None):
    runtime = NativeFixture()
    mission = {'goal': 'Read a fixture journal', 'allowed_origins': ['https://journal.test'], **(mission or {})}
    profile = {'id': 'fixture', 'allow_private_network': True, **(profile or {})}
    context = await create_context(runtime, 'native-session', mission, profile)
    session = SimpleNamespace(id='native-session', context=context, page=context.pages[0],
        policy=context.policy, mission=mission, frame_id=0, epoch=3, control='agent', subscribers=set(),
        last_status=403, challenge_origin='https://journal.test', challenge_url='https://journal.test/article')
    session.page.session = session
    return runtime, session


@pytest.mark.asyncio
async def test_native_observation_is_ocr_not_html_or_response_status():
    runtime, session = await fixture_session()
    runtime.value.update(url='https://journal.test/article', title='Fixture article', text='Observed screenshot text')
    value = await observe(session)
    assert value['text'] == 'Observed screenshot text' and value['elements'] == []
    assert value['observation_kind'] == 'desktop_screenshot_ocr'
    assert value['http_status'] is None and session.last_status is None
    assert value['capabilities']['dom'] is False and value['capabilities']['systematic_inventory'] is False
    assert value['screenshot_sha256'] == hashlib.sha256(runtime.value['png']).hexdigest()
    with pytest.raises(AccessDenied, match='DOM selectors'):
        session.page.locator('body')
    with pytest.raises(AccessDenied, match='publisher HTML'):
        await session.page.content()
    with pytest.raises(AccessDenied, match='JavaScript'):
        await session.page.evaluate('document.body.innerText')
    with pytest.raises(AccessDenied, match='cookies stay'):
        await session.context.cookies()
    with pytest.raises(AccessDenied, match='receipt is unavailable'):
        await session.context.download('https://journal.test/article.pdf', 1000)


@pytest.mark.asyncio
async def test_native_screen_input_and_human_frames_share_coordinates_and_epoch():
    runtime, session = await fixture_session()
    queue = asyncio.Queue(maxsize=1)
    session.subscribers.add(queue)
    response = await session.page.goto('https://journal.test/article')
    assert response is None
    await session.page.mouse.click(220, 310)
    await session.page.keyboard.insert_text('fixture input')
    await session.page.keyboard.press('Tab')
    await session.page.mouse.wheel(0, 100)
    assert [action for _, action, _ in runtime.actions] == ['navigate', 'click', 'type', 'press', 'scroll']
    assert runtime.actions[-1][2] == {'delta_x': 0, 'delta_y': 100}
    # Frame polling and individual input do not steal focus for URL/OCR reads.
    assert runtime.observations == 1
    await session.page.title()
    png = await session.page.screenshot()
    assert runtime.observations == 1
    frame = queue.get_nowait()
    assert (frame['width'], frame['height'], frame['epoch'], frame['control']) == (1280, 800, 3, 'agent')
    assert frame['frame_id'] == 1 and png == runtime.value['png']
    with pytest.raises(AccessDenied, match='outside'):
        await session.page.mouse.click(1280, 40)
    with pytest.raises(AccessDenied, match='shortcut'):
        await session.page.keyboard.press('Control+O')
    await session.context.close()
    await session.context.close()
    assert runtime.closed == ['native-session']


@pytest.mark.asyncio
@pytest.mark.parametrize('mission,profile,reason', [
    ({'allowed_origins': []}, {}, 'finite origin'),
    ({'allowed_origins': ['https://journal.test/secret?key=fixture']}, {}, 'origins, not paths'),
    ({'scope': {'allowed_paths': ['/article']}}, {}, 'path-restricted'),
    ({}, {'challenge_success_selector': 'main'}, 'DOM success selector'),
    ({'external_model_content': 'metadata'}, {}, 'metadata only'),
    ({'retrieval_policy': {'browser_fallback': False}}, {}, 'browser fallback disabled'),
    ({'allowed_origins': ['https://jacc.org'], 'source_policy': {'exclude': {'download': ['jacc']}}}, {}, 'source excluded'),
    ({'allowed_origins': ['https://www.kci.go.kr']}, {}, 'API operation denied'),
])
async def test_native_startup_denies_unrepresentable_or_excluded_policy(mission, profile, reason):
    with pytest.raises(AccessDenied, match=reason):
        await fixture_session(mission, profile)


@pytest.mark.asyncio
async def test_support_origin_is_not_silently_a_native_navigation_permission():
    runtime, session = await fixture_session({'scope': {'browser_support_origins': ['https://support.test']}})
    assert runtime.created[0][1] == ['https://journal.test']
    with pytest.raises(AccessDenied, match='outside'):
        await session.page.goto('https://support.test/privileged')
    assert runtime.actions == []
    runtime.value.update(url='https://outside.test/redirect')
    with pytest.raises(AccessDenied, match='left the permitted origins'):
        await observe(session)
    runtime.value.update(url='https://journal.test/article', url_observed=False)
    with pytest.raises(AccessDenied, match='independently observed'):
        await observe(session)


@pytest.mark.asyncio
@pytest.mark.parametrize('url,title,text,configured,expected', [
    ('https://journal.test/article', 'Article', 'Independently expected article title with original results.', True, True),
    ('https://journal.test/other', 'Article', 'Independently expected article title with original results.', True, False),
    ('https://journal.test/article', 'Article', 'A long unrelated page with more than eighty characters does not prove that the requested journal article loaded.', False, False),
    ('https://journal.test/article', 'Just a moment...', 'Independently expected article title. Verify you are human.', True, False),
    ('https://journal.test/article', 'Error', 'Independently expected article title. ERR_CONNECTION_REFUSED.', True, False),
    ('https://journal.test/article', 'Sign in', 'Independently expected article title. Enter your password.', True, False),
])
async def test_native_recovery_requires_configured_target_evidence(url, title, text, configured, expected):
    runtime, session = await fixture_session(profile={'desktop_success_text': ['Independently expected article title']} if configured else {})
    runtime.value.update(url=url, title=title, text=text)
    recovered, evidence = await target_recovered(session)
    assert recovered is expected
    if recovered:
        assert evidence['status'] is None and evidence['http_status_observed'] is False
        assert evidence['target_markers_observed'] == 1 and len(evidence['ocr_sha256']) == 64
        assert 'Independently expected' not in str(evidence)


class DownloadFixture(NativeFixture):
    def __init__(self, directory, content=b'column,value\nfixture,7\n'):
        super().__init__()
        self.directory = directory
        directory.mkdir()
        path = directory / 'table.csv'
        path.write_bytes(content)
        self.pending = {'id': 9, 'path': str(path), 'filename': path.name, 'bytes': len(content),
            'state': 1, 'complete': True, 'sha256': hashlib.sha256(content).hexdigest(),
            'url_chain': ['https://journal.test/download'], 'provenance': 'chrome_download_history'}
        self.available = False

    async def downloads(self, sid):
        return [dict(self.pending)] if self.available else []

    async def action(self, sid, action, args):
        await super().action(sid, action, args)
        if action == 'navigate':
            self.available = True

    def get(self, sid):
        return SimpleNamespace(download_dir=self.directory)


@pytest.mark.asyncio
async def test_native_download_uses_actual_completed_receipt_and_chain(tmp_path):
    runtime = DownloadFixture(tmp_path / 'downloads')
    runtime.pending['url_chain'].append('https://journal.test/final.csv')
    context = await create_context(runtime, 'download', {'allowed_origins': ['https://journal.test']},
                                   {'allow_private_network': True})
    data, receipt = await context.download('https://journal.test/download', 1000)
    assert data == b'column,value\nfixture,7\n'
    assert receipt['url'] == 'https://journal.test/download'
    assert receipt['final_url'] == 'https://journal.test/final.csv'
    assert receipt['redirect_chain'] == runtime.pending['url_chain']
    assert receipt['receipt_kind'] == 'chrome_download_history'
    assert receipt['sha256'] == hashlib.sha256(data).hexdigest() and receipt['http_status'] is None
    assert 'path' not in receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['stale', 'wrong_url', 'redirect', 'hash', 'size', 'symlink'])
async def test_native_download_rejects_stale_unrelated_or_unsafe_receipt(tmp_path, failure):
    runtime = DownloadFixture(tmp_path / 'downloads')
    if failure == 'stale':
        runtime.available = True
    elif failure == 'wrong_url':
        runtime.pending['url_chain'] = ['https://journal.test/unrelated']
    elif failure == 'redirect':
        runtime.pending['url_chain'].append('https://outside.test/file')
    elif failure == 'hash':
        runtime.pending['sha256'] = '0' * 64
    elif failure == 'size':
        runtime.pending['bytes'] += 1
    elif failure == 'symlink':
        external = tmp_path / 'outside.csv'
        external.write_bytes(b'column,value\nfixture,7\n')
        from pathlib import Path
        path = Path(runtime.pending['path'])
        path.unlink()
        path.symlink_to(external)
    mission = {'allowed_origins': ['https://journal.test'], 'limits': {'native_download_timeout_seconds': .01}}
    context = await create_context(runtime, 'download', mission, {'allow_private_network': True})
    with pytest.raises(AccessDenied):
        await context.download('https://journal.test/download', 1000)


@pytest.mark.asyncio
async def test_native_download_cannot_act_during_human_control(tmp_path):
    runtime = DownloadFixture(tmp_path / 'downloads')
    context = await create_context(runtime, 'download', {'allowed_origins': ['https://journal.test']},
                                   {'allow_private_network': True})
    context.pages[0].session = SimpleNamespace(control='human', lock=asyncio.Lock())
    with pytest.raises(AccessDenied, match='controlled by the user'):
        await context.download('https://journal.test/download', 1000)
    assert not runtime.actions


async def download_ingestion_fixture(tmp_path, budget=1000):
    from ore.store import Store
    runtime = DownloadFixture(tmp_path / 'downloads')
    runtime.available = True
    mission = {'goal': 'Download fixture', 'allowed_origins': ['https://journal.test'], 'budget': {'max_bytes': budget}}
    store = Store('sqlite:///' + str(tmp_path / 'fixture.sqlite'))
    store.initialize()
    job = store.create_job(mission)
    context = await create_context(runtime, 'download', mission, {'allow_private_network': True})
    session = SimpleNamespace(id='download', job_id=job['id'], context=context, page=context.pages[0],
                              downloads=[], lock=asyncio.Lock(), control='agent')
    context.pages[0].session = session
    return runtime, session, store, job


@pytest.mark.asyncio
async def test_native_click_download_is_staged_and_charged_once_across_polls(tmp_path):
    from ore.desktop_browser import collect_downloads
    runtime, session, store, job = await download_ingestion_fixture(tmp_path)
    try:
        async with session.lock:
            first = await collect_downloads(session, tmp_path / 'staging', store)
            second = await collect_downloads(session, tmp_path / 'staging', store)
        assert len(first) == 1 and second == [] and len(session.downloads) == 1
        item = first[0]
        assert item['status'] == 'staged' and item['receipt_kind'] == 'chrome_download_history'
        assert item['requested_source_url'] == 'https://journal.test/download'
        from pathlib import Path
        assert Path(item['path']).parent == tmp_path / 'staging'
        assert Path(item['path']).read_bytes() == b'column,value\nfixture,7\n'
        used = store.get_budget(f"{job['id']}:{job['revision']}:{job['generation']}", 'download_bytes')
        assert used['used'] == item['bytes']
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['foreign_origin', 'changed_file', 'budget'])
async def test_native_click_download_invalid_receipt_never_enters_staging(tmp_path, failure):
    from ore.desktop_browser import collect_downloads
    runtime, session, store, job = await download_ingestion_fixture(tmp_path, budget=1 if failure == 'budget' else 1000)
    if failure == 'foreign_origin':
        runtime.pending['url_chain'].append('https://outside.test/file')
    elif failure == 'changed_file':
        runtime.pending['sha256'] = '0' * 64
    try:
        async with session.lock:
            with pytest.raises(AccessDenied):
                await collect_downloads(session, tmp_path / 'staging', store)
        assert not session.downloads and not session.context.consumed_downloads
        assert not (tmp_path / 'staging').exists()
        used = store.get_budget(f"{job['id']}:{job['revision']}:{job['generation']}", 'download_bytes')
        assert (used or {'used': 0})['used'] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_explicit_native_download_is_not_ingested_again_by_observe(tmp_path):
    from ore.desktop_browser import collect_downloads
    runtime, session, store, job = await download_ingestion_fixture(tmp_path)
    runtime.available = False
    try:
        data, receipt = await session.context.download('https://journal.test/download', 1000)
        assert receipt['native_download_id'] in session.context.consumed_downloads
        # Explicit-download caller owns its byte reservation; observe cannot charge it again.
        async with session.lock:
            assert await collect_downloads(session, tmp_path / 'staging', store) == []
        assert not session.downloads
        assert store.get_budget(f"{job['id']}:{job['revision']}:{job['generation']}", 'download_bytes') is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_publish_frame_uses_the_exact_observation_image_without_recapture():
    import base64
    runtime, session = await fixture_session()
    runtime.value.update(url='https://journal.test/article', text='Original OCR text')
    observation = await observe(session)
    captured = session.page.latest['png']
    async def forbidden_recapture(sid):
        raise AssertionError('Publishing an observation must not recapture a newer screen')
    runtime.screenshot = forbidden_recapture
    await session.page.publish_frame(captured)
    assert hashlib.sha256(base64.b64decode(session.frame['data'])).hexdigest() == observation['screenshot_sha256']
    assert runtime.observations == 1

@pytest.mark.parametrize('url,text', [
    ('https://www.jacc.org/toc/jacc/83/1', 'JACC Vol. 83 No. 1 January 2, 2024'),
    ('https://www.ahajournals.org/toc/circ/149/1', 'Vol 149, No 1 | Circulation'),
    ('https://academic.oup.com/eurheartj/issue/45/1', 'European Heart Journal Volume 45 Issue 1'),
    ('https://jamanetwork.com/journals/jamacardiology/issue/9/1', 'JAMA Cardiology Volume 9 Number 1'),
])
def test_known_issue_recovery_requires_brand_volume_issue_and_exact_checkpoint(url, text):
    from ore.desktop_browser import issue_checkpoint_observed
    assert issue_checkpoint_observed(url, url, text)
    assert not issue_checkpoint_observed(url, url.rsplit('/', 1)[0] + '/2', text)
    assert not issue_checkpoint_observed(url, url, 'Just a moment. Verify you are human')
    assert not issue_checkpoint_observed(url, url, text.replace('1', '2'))
    assert not issue_checkpoint_observed('https://unrelated.test/issue/1', url, text)

@pytest.mark.asyncio
async def test_native_issue_handoff_recovery_rejects_challenge_even_with_visible_title():
    url = 'https://www.jacc.org/toc/jacc/83/1'
    runtime, session = await fixture_session(mission={'allowed_origins': ['https://www.jacc.org'], 'desktop_issue_checkpoint': url})
    session.challenge_origin = 'https://www.jacc.org'; session.challenge_url = url
    runtime.value.update(url=url, title='JACC Vol. 83 No. 1', text='JACC Vol. 83 No. 1')
    assert (await target_recovered(session))[0]
    runtime.value['text'] += '\nVerify you are human'
    assert not (await target_recovered(session))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('configured,expected', [(None, '4g'), ('6g', '6g')])
async def test_browser_settings_memory_reaches_native_container_limits(tmp_path, monkeypatch, configured, expected):
    import json
    from ore.browser import BrowserManager
    from ore.config import Settings
    from ore.desktop import DesktopRuntime
    from ore.policy import RateLimiter

    if configured is None:
        monkeypatch.delenv('ORE_DESKTOP_MEMORY', raising=False)
    else:
        monkeypatch.setenv('ORE_DESKTOP_MEMORY', configured)
    settings = Settings(state_dir=tmp_path / 'state', auth_token='fixture',
                        browser_backend='desktop_chrome', desktop_resource_mode='cgroup').prepare()
    calls = []
    async def command(runtime, args, **kwargs):
        calls.append(args)
        if args[0] == 'exec':
            request = json.loads(kwargs['data'])
            assert request['action'] == 'ready'
            return b'{"ready": true}'
        return b'fixture-container'
    monkeypatch.setattr(DesktopRuntime, '_command', command)
    manager = BrowserManager(settings, RateLimiter())
    try:
        session = await manager.create('fixture-job',
            {'goal': 'Read fixture', 'allowed_origins': ['https://journal.test']},
            {'id': 'fixture', 'allow_private_network': True})
        launch = next(args for args in calls if args[0] == 'run')
        assert launch[launch.index('--memory') + 1] == expected
        assert launch[launch.index('--memory-swap') + 1] == expected
        assert '--pids-limit' in launch and '--cpus' in launch
        policies = list((settings.state_dir / 'desktop' / 'policies').glob('*.json'))
        assert len(policies) == 1
        assert json.loads(policies[0].read_text())['GenAILocalFoundationalModelSettings'] == 1
        assert session.context.desktop
    finally:
        await manager.close()
    assert len([args for args in calls if args[0] == 'stop']) == 1


@pytest.mark.asyncio
async def test_invalid_configured_memory_fails_before_starting_a_desktop(tmp_path, monkeypatch):
    from ore.browser import BrowserManager
    from ore.config import Settings
    from ore.desktop import DesktopRuntime
    from ore.policy import RateLimiter
    from unittest.mock import AsyncMock
    monkeypatch.setenv('ORE_DESKTOP_MEMORY', '0g')
    settings = Settings(state_dir=tmp_path / 'state', auth_token='fixture',
                        browser_backend='desktop_chrome').prepare()
    command = AsyncMock()
    monkeypatch.setattr(DesktopRuntime, '_command', command)
    manager = BrowserManager(settings, RateLimiter())
    try:
        with pytest.raises(ValueError, match='Invalid desktop resource limits'):
            await manager.create('fixture-job',
                {'goal': 'Read fixture', 'allowed_origins': ['https://journal.test']},
                {'id': 'fixture', 'allow_private_network': True})
        command.assert_not_awaited()
        assert not manager.sessions
    finally:
        await manager.close()
