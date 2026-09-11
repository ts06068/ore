"""Screenshot/OS-input browser facade; no DOM, CDP, or exported browser cookies.

Native observations are operational evidence only. OCR does not become rendered
HTML, a publisher response status, or a sealed systematic article inventory.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import nullcontext
import hashlib
import math
import os
import re
from pathlib import Path
import stat
import struct
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

from .policy import AccessDenied, AccessPolicy, redact
from .source_policy import authorize_operation, operation_for_url, source_for_url

CAPABILITIES = {
    'dom': False, 'screenshot': True, 'coordinate_input': True,
    'http_status': False, 'cookie_export': False, 'native_download_receipts': True,
    'systematic_inventory': False, 'network_enforcement': 'navigation_origin_policy',
}
_CHALLENGE_TEXT = ('verify you are human', 'verifying you are human', 'checking your browser',
    'just a moment', 'performing security verification', 'complete the security check',
    'review the security of your connection', 'verify that you are human')
_ERROR_TEXT = ('err_connection_', 'err_name_not_resolved', 'err_blocked_', 'err_cert_',
    'this site can’t be reached', "this site can't be reached", 'access denied',
    'you have been blocked', 'automated browser detected', 'incompatible browser extension')
_AUTH_TEXT = ('enter your password', 'enter password', 'enter your institution credentials')


def _origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        if '*' in parsed.netloc or parsed.port is not None and not 0 < parsed.port < 65536:
            raise ValueError()
        return f'{parsed.scheme}://{parsed.netloc}'
    except (TypeError, ValueError):
        raise AccessDenied('Desktop browser requires an exact HTTP(S) origin without credentials') from None


async def desktop_origins(mission, profile):
    """Admit only policies that a finite native navigation allowlist can express."""
    scope = mission.get('scope') or {}
    origins = mission.get('allowed_origins') or scope.get('origins') or []
    if not isinstance(origins, list) or not origins:
        raise AccessDenied('Desktop browser requires an explicit finite origin allowlist')
    if profile.get('proxy') or profile.get('headers'):
        raise AccessDenied('Desktop browser does not support profile proxy or request-header overrides')
    if profile.get('challenge_success_selector'):
        raise AccessDenied('Desktop browser requires desktop_success_text instead of a DOM success selector')
    for config in (mission, scope, profile):
        if any(config.get(key) for key in ('allowed_paths', 'denied_paths', 'path_patterns',
                'url_allowlist', 'url_denylist', 'allowed_url_patterns', 'denied_url_patterns')):
            raise AccessDenied('Desktop browser cannot enforce path-restricted access policies')
    result = []
    for url in origins:
        origin = _origin(url)
        if url.rstrip('/') != origin:
            raise AccessDenied('Desktop allowlist entries must be origins, not paths or queries')
        for operation in ('browser', 'download'):
            await AccessPolicy(mission, profile, operation=operation).check(origin + '/')
        # A page and API can share a host but have different source operations.
        # Do not expose those paths when a native origin rule cannot distinguish them.
        for suffix in ('/', '/openapi/', '/oai/', '/piaapi/', '/api/', '/pmc/utils/', '/oa.fcgi', '/idconv/'):
            target = origin + suffix
            operation = operation_for_url(target, 'browser')
            if operation != 'browser':
                decision = authorize_operation(mission, profile, source_for_url(target, profile), operation, url=target)
                if not decision['allowed']:
                    raise AccessDenied('Desktop origin contains an API operation denied by the source policy')
        result.append(origin)
    return sorted(set(result))


def _png(data):
    if isinstance(data, str):
        try:
            data = base64.b64decode(data.removeprefix('data:image/png;base64,'), validate=True)
        except ValueError:
            raise AccessDenied('Invalid native screenshot encoding') from None
    if not isinstance(data, bytes) or not 24 <= len(data) <= 8 * 1024 * 1024 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise AccessDenied('Invalid native screenshot')
    if struct.unpack('>II', data[16:24]) != (1280, 800):
        raise AccessDenied('Native screenshot must match the 1280 by 800 input surface')
    return data


def _fold(value):
    return ' '.join(str(value).casefold().split())


def _page_state(text, title):
    combined = _fold(title + '\n' + text)
    if any(marker in combined for marker in _ERROR_TEXT):
        return 'error'
    if any(marker in combined for marker in _CHALLENGE_TEXT):
        return 'challenge'
    if _fold(title) in ('sign in', 'log in', 'login') or any(marker in combined for marker in _AUTH_TEXT):
        return 'authentication'
    return 'content' if text.strip() else 'unknown'


class DesktopPage:
    def __init__(self, context):
        self.context, self.session, self.url = context, None, 'about:blank'
        self.latest = {}
        self.mouse = SimpleNamespace(click=self.click, wheel=self.scroll)
        self.keyboard = SimpleNamespace(press=self.key, insert_text=self.type_text)

    async def _refresh(self):
        value = await self.context.runtime.observe(self.context.id)
        if not isinstance(value, dict) or not value.get('url_observed') or not isinstance(value.get('url'), str):
            raise AccessDenied('Native browser URL could not be independently observed')
        url = value['url']
        if url != 'about:blank':
            if _origin(url) not in self.context.origins:
                raise AccessDenied('Native Chrome left the permitted origins; return to the requested page')
            await self.context.policy.check(url)
        data = _png(value.get('png'))
        self.url = url
        self.latest = {'url': url, 'url_observed': True, 'text': str(value.get('text', ''))[:30000],
            'title': str(value.get('title', ''))[:1000], 'png': data,
            'screenshot_sha256': hashlib.sha256(data).hexdigest()}
        if self.session:
            self.session.last_status = None
        return self.latest

    async def _action(self, action, arguments):
        if self.context.closed:
            raise AccessDenied('Native browser session is closed')
        # Ordinary input must not trigger omnibox/OCR observation: doing so
        # would steal focus between individual human keystrokes.
        return await self.context.runtime.action(self.context.id, action, arguments)

    async def goto(self, url, **kwargs):
        if _origin(url) not in self.context.origins:
            raise AccessDenied('Native navigation is outside the permitted origins')
        await self.context.policy.check(url)
        await self._action('navigate', {'url': url})
        await self._refresh()
        return None  # There is no observed HTTP response, including for successful navigation.

    async def click(self, x, y):
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y)) or not (0 <= x < 1280 and 0 <= y < 800):
            raise AccessDenied('Native click is outside the observed screen')
        await self._action('click', {'x': x, 'y': y})

    async def scroll(self, deltaX, deltaY):
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and abs(v) <= 10000 for v in (deltaX, deltaY)):
            raise AccessDenied('Invalid native scroll distance')
        await self._action('scroll', {'delta_x': deltaX, 'delta_y': deltaY})

    async def key(self, key):
        # App/window/file/developer shortcuts are not part of page interaction.
        denied = {'control+l', 'ctrl+l', 'alt+d', 'control+o', 'ctrl+o', 'control+n', 'ctrl+n',
            'control+t', 'ctrl+t', 'control+shift+i', 'ctrl+shift+i', 'control+shift+j', 'ctrl+shift+j',
            'control+shift+n', 'ctrl+shift+n', 'f6', 'f11', 'f12', 'alt+f4', 'alt+tab', 'super', 'meta'}
        if not isinstance(key, str) or len(key) > 80 or key.casefold() in denied or 'super+' in key.casefold() or 'meta+' in key.casefold():
            raise AccessDenied('Native browser shortcut is not permitted')
        await self._action('press', {'key': key})

    async def type_text(self, text):
        if not isinstance(text, str) or len(text) > 10000:
            raise AccessDenied('Native text input exceeds the per-action limit')
        await self._action('type', {'text': text})

    async def title(self):
        return self.latest.get('title', '')

    async def publish_frame(self, png):
        """Publish this exact captured image; do not recapture or read the URL."""
        data = _png(png)
        if self.session:
            session = self.session
            session.frame_id += 1
            session.frame = {'type': 'frame', 'data': base64.b64encode(data).decode(),
                'width': 1280, 'height': 800, 'frame_id': session.frame_id,
                'epoch': session.epoch, 'control': session.control, 'scale': 1}
            for queue in list(session.subscribers):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(dict(session.frame))
        return data

    async def screenshot(self, **kwargs):
        if kwargs.get('mask'):
            raise AccessDenied('DOM masking is unavailable for a native desktop screenshot')
        return await self.publish_frame(await self.context.runtime.screenshot(self.context.id))

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(max(0, min(float(ms), 10000)) / 1000)

    async def go_back(self, **kwargs):
        await self._action('press', {'key': 'Alt+Left'})

    async def bring_to_front(self):
        # This backend has one browser window; runtime actions focus it atomically.
        return None

    def locator(self, *args, **kwargs):
        raise AccessDenied('Desktop Chrome provides screenshots and coordinates, not DOM selectors')

    async def content(self):
        raise AccessDenied('Native OCR cannot be exported as publisher HTML')

    async def evaluate(self, *args, **kwargs):
        raise AccessDenied('JavaScript evaluation is unavailable in native desktop Chrome')


class DesktopContext:
    desktop = True
    capabilities = CAPABILITIES

    def __init__(self, runtime, sid, mission, profile, origins):
        self.runtime, self.id, self.origins = runtime, sid, origins
        self.policy, self.closed = AccessPolicy(mission, profile), False
        self.download_lock = asyncio.Lock()
        self.consumed_downloads = set()
        self.pages = [DesktopPage(self)]

    async def close(self):
        if not self.closed:
            await self.runtime.close_session(self.id)
            self.closed = True

    async def cookies(self, *args, **kwargs):
        raise AccessDenied('Native Chrome cookies stay inside its isolated profile')

    async def storage_state(self):
        return {'cookies': [], 'origins': []}

    async def download(self, url, limit):
        if not callable(getattr(self.runtime, 'downloads', None)):
            raise AccessDenied('Native download receipt is unavailable; an observed completed Chrome file is required')
        if type(limit) is not int or limit <= 0:
            raise AccessDenied('Native download byte limit must be positive')
        mission, profile = self.policy.mission, self.policy.profile
        limit = min(limit, int(mission.get('limits', {}).get('max_artifact_bytes', 256 * 1024 * 1024)))
        timeout = min(60.0, max(0.01, float(mission.get('limits', {}).get('native_download_timeout_seconds', 45))))
        policy = AccessPolicy(mission, profile, operation='download')
        await policy.check(url)
        if _origin(url) not in self.origins:
            raise AccessDenied('Native file URL is outside the permitted navigation origins')
        async with self.download_lock:
            session = self.pages[0].session
            async with session.lock if session is not None and hasattr(session, 'lock') else nullcontext():
                if session is not None and session.control != 'agent':
                    raise AccessDenied('Native browser is controlled by the user')
                baseline = {str(item['id']) for item in await self.runtime.downloads(self.id)}
                await self.pages[0]._action('navigate', {'url': url})
                deadline = asyncio.get_running_loop().time() + timeout
                while asyncio.get_running_loop().time() < deadline:
                    if session is not None and (session.control != 'agent' or getattr(session, 'closed', False)):
                        raise AccessDenied('Native browser control changed during the download')
                    for item in await self.runtime.downloads(self.id):
                        chain = item.get('url_chain')
                        if str(item.get('id')) in baseline or not isinstance(chain, list) or not chain or chain[0] != url:
                            continue
                        if item.get('provenance') != 'chrome_download_history' or item.get('complete') is not True or item.get('state') != 1:
                            continue
                        data, metadata = await self.verified_receipt(item, limit)
                        self.consumed_downloads.add(str(item['id']))
                        return data, metadata
                    await asyncio.sleep(min(.25, max(0, deadline - asyncio.get_running_loop().time())))
                raise AccessDenied('Native download has no new completed Chrome file receipt within the time limit')


    async def verified_receipt(self, item, limit):
        chain = item.get('url_chain')
        if (item.get('provenance') != 'chrome_download_history' or item.get('complete') is not True
                or type(item.get('state')) is not int or item['state'] != 1
                or not isinstance(chain, list) or not chain or any(not isinstance(target, str) for target in chain)):
            raise AccessDenied('Native completed download receipt is malformed')
        policy = AccessPolicy(self.policy.mission, self.policy.profile, operation='download')
        for target in chain:
            await policy.check(target)
            if _origin(target) not in self.origins:
                raise AccessDenied('Native download redirect left the permitted origins')
        native = self.runtime.get(self.id)
        data = await asyncio.to_thread(_read_receipted_file, item, native.download_dir, limit)
        return data, {'url': chain[0], 'final_url': chain[-1], 'redirect_chain': list(chain),
            'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'receipt_kind': 'chrome_download_history', 'native_download_id': str(item['id']),
            'filename': item.get('filename'), 'http_status': None}


def _read_receipted_file(receipt, directory, limit):
    directory = Path(directory).resolve()
    path = Path(receipt.get('path', ''))
    if path.is_symlink() or path.parent.resolve() != directory or path.name.endswith(('.crdownload', '.tmp')):
        raise AccessDenied('Native file is outside the completed download directory')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit or before.st_size != receipt.get('bytes'):
            raise AccessDenied('Native completed-file size exceeds the limit or differs from its receipt')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) != before.st_size or len(data) > limit
            or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
               != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or hashlib.sha256(data).hexdigest() != receipt.get('sha256')):
        raise AccessDenied('Native completed file changed or failed its receipt hash')
    return data


async def collect_downloads(session, staging_dir, store):
    """Called while the manager owns session.lock; never reacquire that lock."""
    context = session.context
    if not callable(getattr(context.runtime, 'downloads', None)):
        return []
    receipts = await context.runtime.downloads(context.id)
    if not receipts:
        return []
    if store is None:
        raise AccessDenied('Native download ingestion requires durable byte accounting')
    job = await asyncio.to_thread(store.get_job, session.job_id)
    if not job:
        raise AccessDenied('Native download job is unavailable')
    mission = job['mission']
    maximum = int(mission.get('limits', {}).get('max_artifact_bytes', 256 * 1024 * 1024))
    scope = f"{job['id']}:{job['revision']}:{job['generation']}"
    staged = []
    for item in receipts:
        ident = str(item.get('id'))
        if ident in context.consumed_downloads:
            continue
        # Recheck the current mission as well as the session's original scope.
        chain = item.get('url_chain')
        if not isinstance(chain, list) or not chain:
            raise AccessDenied('Native completed download receipt has no source chain')
        current = AccessPolicy(mission, context.policy.profile, operation='download')
        for target in chain:
            await current.check(target)
        data, metadata = await context.verified_receipt(item, maximum)
        budget = await asyncio.to_thread(store.reserve_budget, scope, 'download_bytes', len(data),
            mission.get('budget', {}).get('max_bytes', 1_000_000_000))
        if not budget['allowed']:
            raise AccessDenied('Download byte budget exceeded')
        directory = Path(staging_dir)
        if directory.is_symlink():
            raise AccessDenied('Native staging directory cannot be a symlink')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / uuid.uuid4().hex
        with path.open('xb') as output:
            output.write(data)
        value = {'status': 'staged', 'session_id': session.id, 'path': str(path),
            'filename': metadata['filename'] or Path(item['path']).name,
            'bytes': len(data), 'sha256': metadata['sha256'], 'url': metadata['final_url'],
            'requested_source_url': metadata['url'], 'redirect_chain': metadata['redirect_chain'],
            'observed_page_url': session.page.url, 'receipt_kind': metadata['receipt_kind'],
            'native_download_id': metadata['native_download_id'], 'http_status': None}
        session.downloads.append(value)
        context.consumed_downloads.add(ident)
        staged.append(value)
    return staged


async def create_context(runtime, sid, mission, profile):
    origins = await desktop_origins(mission, profile)
    if mission.get('external_model_content') == 'metadata':
        raise AccessDenied('Native browser requires screenshot content; this mission permits metadata only')
    await runtime.create_session(sid, allowed_origins=origins,
        task_budget_seconds=mission.get('budget',{}).get('max_seconds',300))
    return DesktopContext(runtime, sid, mission, profile, origins)


async def observe(session):
    value = await session.page._refresh()
    state = _page_state(value['text'], value['title'])
    return {'url': redact(value['url']), 'title': value['title'], 'text': value['text'],
        'elements': [], 'tabs': [{'index': 0, 'url': redact(value['url'])}],
        'challenge_detected': any(marker in _fold(value['title'] + '\n' + value['text'])
            for marker in (*_CHALLENGE_TEXT, 'automated browser detected', 'incompatible browser extension')), 'page_state': state,
        'http_status': None, 'observation_kind': 'desktop_screenshot_ocr',
        'screenshot_sha256': value['screenshot_sha256'], 'capabilities': dict(CAPABILITIES),
        'evidence_limitations': ['OCR is not publisher HTML.', 'HTTP status and DOM are not observed.',
            'Screenshot observations do not establish systematic inventory completeness.']}


async def challenge_visible(session):
    return (await observe(session))['challenge_detected']


def issue_checkpoint_identity(url):
    """Known publisher issue paths; this identifies access, never an inventory."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    formats = {
        'www.jacc.org': (r'/toc/jacc/(\d+)/(\d+)/?', (r'\bjacc\b',)),
        'www.ahajournals.org': (r'/toc/circ/(\d+)/(\d+)/?', (r'\bcirculation\b',)),
        'academic.oup.com': (r'/eurheartj/issue/(\d+)/(\d+)/?', (r'\beuropean heart journal\b',)),
        'jamanetwork.com': (r'/journals/jamacardiology/issue/(\d+)/(\d+)/?', (r'\bjama cardiology\b',)),
    }
    rule = formats.get(parsed.hostname)
    match = re.fullmatch(rule[0], parsed.path) if rule else None
    return (rule[1], int(match[1]), int(match[2])) if match else None


def issue_checkpoint_observed(checkpoint, url, text):
    identity = issue_checkpoint_identity(checkpoint)
    a, b = urlsplit(checkpoint), urlsplit(url)
    if not identity or (a.scheme, a.netloc, a.path) != (b.scheme, b.netloc, b.path):
        return False
    brands, volume, issue = identity
    folded = _fold(text)
    volume_label = rf'\bvol(?:ume)?\.?\s*{volume}\b'
    issue_label = rf'\b(?:no\.?|number|issue)\s*{issue}\b'
    return (any(re.search(pattern, folded) for pattern in brands)
            and bool(re.search(volume_label, folded)) and bool(re.search(issue_label, folded)))


def ehj_archive_checkpoint_observed(checkpoint, url, title, text):
    """Recognize a specific official archive view, never a sealed issue inventory."""
    try:
        expected, actual = urlsplit(checkpoint), urlsplit(url)
        if (expected.scheme != 'https' or expected.hostname != 'academic.oup.com'
                or expected.username or expected.password or expected.port not in (None, 443)
                or (expected.scheme, expected.netloc, expected.path) != (actual.scheme, actual.netloc, actual.path)):
            return False
    except (TypeError, ValueError):
        return False
    path = re.fullmatch(r'/eurheartj/issue-archive(?:/(\d{4}))?/?', expected.path)
    if not path:
        return False
    heading, body = _fold(title), _fold(text)
    if not all(re.search(pattern, heading) for pattern in
               (r'\beuropean heart journal\b', r'\boxford academic\b')):
        return False
    if path[1]:
        label = rf'\b{path[1]}\s+issues\b'
        return bool(re.search(label, heading) and re.search(label, body)
            and re.search(r'\bvol(?:ume)?\.?\s*\d+\s*[,;:]?\s+issue\s+\d+\b', body))
    years = set(re.findall(r'\b(?:19[89]\d|20\d{2})\b', body))
    return bool(re.search(r'\ball\s+issues\b', heading)
        and re.search(r'\ball\s+issues\b', body) and len(years) >= 2)


async def target_recovered(session):
    value = await observe(session)
    if value['page_state'] != 'content':
        return False, {'reason': 'native_' + value['page_state'] + '_still_observed'}
    url = session.page.url
    if session.challenge_origin and _origin(url) != session.challenge_origin:
        return False, {'reason': 'different_origin_is_not_resolution'}
    if session.challenge_url and urlsplit(session.challenge_url).path != urlsplit(url).path:
        return False, {'reason': 'different_page_is_not_resolution'}
    markers = session.policy.profile.get('desktop_success_text') or session.mission.get('desktop_success_text')
    if isinstance(markers, str):
        markers = [markers]
    checkpoint = session.mission.get('desktop_issue_checkpoint')
    if checkpoint:
        # A known issue requires its own volume and issue evidence even when
        # the session previously used generic homepage or profile markers.
        matches = issue_checkpoint_observed(checkpoint, url, value['title'] + '\n' + value['text'])
        marker_count = 3  # observed brand, volume and issue at the exact checkpoint
    elif (session.mission.get('desktop_context', {}).get('protocol_id') == 'journal.ehj'
            and re.fullmatch(r'/eurheartj/issue-archive(?:/\d{4})?/?', urlsplit(session.challenge_url or url).path)):
        # The cropped publisher logo is not reliable OCR. Only these exact
        # packaged archive views may use the fully branded browser title,
        # alongside archive-specific body evidence; arbitrary pages may not.
        matches = ehj_archive_checkpoint_observed(session.challenge_url or url, url, value['title'], value['text'])
        marker_count = 4  # journal, publisher, archive heading and archive entries
        generated = (not session.policy.profile.get('desktop_success_text')
            and session.mission.get('desktop_success_text_source') == 'journal_browser_context'
            and markers == ['European Heart Journal', 'Oxford Academic'])
        if not generated:
            # Explicit operator/profile markers remain additional requirements.
            if not isinstance(markers, list) or not markers or any(not isinstance(marker, str) or len(marker.strip()) < 8 for marker in markers):
                return False, {'reason': 'native_target_evidence_not_configured'}
            matches = matches and all(_fold(marker) in _fold(value['text']) for marker in markers)
            marker_count += len(markers)
    else:
        if not isinstance(markers, list) or not markers or any(not isinstance(marker, str) or len(marker.strip()) < 8 for marker in markers):
            return False, {'reason': 'native_target_evidence_not_configured'}
        matches = all(_fold(marker) in _fold(value['text']) for marker in markers)
        marker_count = len(markers)
    if not matches:
        return False, {'reason': 'native_target_markers_not_observed'}
    return True, {'url': redact(url), 'status': None, 'capture_kind': 'desktop_screenshot_ocr',
        'screenshot_sha256': value['screenshot_sha256'], 'ocr_sha256': hashlib.sha256(value['text'].encode()).hexdigest(),
        'target_markers_observed': marker_count, 'substantive_content_observed': True,
        'http_status_observed': False}
