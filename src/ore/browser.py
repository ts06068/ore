"""Server-side browser sessions with fenced control and encrypted session state."""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import os
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Error as PlaywrightError

from .config import SecretStore
from .models import canonical_digest
from .policy import AccessDenied, AccessPolicy, DNSResolutionFailed, RateLimiter, redact, browser_resource_interval
from .browser_diagnostics import classify_network_observation, classify_runtime_diagnostic
from .store import ControlConflict


def _profile_state_entries(state):
    """Index browser storage without exposing cookie or local-storage values."""
    if not isinstance(state, dict):
        raise ValueError("Browser storage state must be an object")
    entries = {}
    for cookie in state.get("cookies", []):
        if not isinstance(cookie, dict) or not all(isinstance(cookie.get(k), str) for k in ("name", "domain", "path")):
            raise ValueError("Invalid browser cookie state")
        partition = json.dumps(cookie.get("partitionKey"), sort_keys=True)
        key = ("cookie", cookie["domain"], cookie["path"], cookie["name"], partition)
        if key in entries:
            raise ValueError("Duplicate browser cookie identity")
        entries[key] = deepcopy(cookie)
    for origin in state.get("origins", []):
        if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
            raise ValueError("Invalid browser origin state")
        name = origin["origin"]
        marker = ("origin", name)
        if marker in entries:
            raise ValueError("Duplicate browser storage origin")
        entries[marker] = True
        for item in origin.get("localStorage", []):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("value"), str):
                raise ValueError("Invalid browser local-storage state")
            key = ("local_storage", name, item["name"])
            if key in entries:
                raise ValueError("Duplicate browser local-storage identity")
            entries[key] = deepcopy(item)
        for key, value in origin.items():
            if key not in ("origin", "localStorage"):
                entries[("origin_field", name, key)] = deepcopy(value)
    for key, value in state.items():
        if key not in ("cookies", "origins"):
            entries[("state_field", key)] = deepcopy(value)
    return entries


def merge_storage_state(baseline, current, latest):
    """Merge observed session changes; deletions win conflicting stale updates.

    Unchanged local values never restore a value another session removed.
    Conflicting concurrent updates preserve the latest persisted value and are
    reported by count only. Each caller retains its local snapshot as baseline.
    """
    before, local, stored = map(_profile_state_entries, (baseline, current, latest))
    merged = deepcopy(stored)
    absent = object()
    conflicts = 0
    for key in before.keys() | local.keys():
        old, new, newest = before.get(key, absent), local.get(key, absent), stored.get(key, absent)
        if new == old or new == newest:
            continue
        if newest != old:
            conflicts += 1
            # A concurrent deletion must not be undone by a stale credential.
            if new is not absent and newest is not absent:
                continue
        if new is absent or newest is absent and newest != old:
            merged.pop(key, None)
        else:
            merged[key] = deepcopy(new)
    result = {"cookies": [], "origins": []}
    origins = {}
    for key, value in sorted(merged.items()):
        kind = key[0]
        if kind == "cookie":
            result["cookies"].append(value)
        elif kind == "state_field":
            result[key[1]] = value
        else:
            origin = origins.setdefault(key[1], {"origin": key[1], "localStorage": []})
            if kind == "local_storage":
                origin["localStorage"].append(value)
            elif kind == "origin_field":
                origin[key[2]] = value
    result["origins"] = [origins[key] for key in sorted(origins)]
    changed = sum(stored.get(key, absent) != merged.get(key, absent) for key in stored.keys() | merged.keys())
    return result, {"saved": True, "status": "saved_with_conflicts" if conflicts else "saved",
                    "conflict_count": conflicts, "changed_entries": changed,
                    "conflict_resolution": "preserve_latest_updates_and_deletions"}


def merge_browser_profile(secrets, ref, baseline, current):
    """Perform one encrypted read/merge/write transaction on the coordinator."""
    if not isinstance(ref, str) or not ref.startswith("browser-profile:"):
        raise ValueError("Browser profile persistence requires a profile reference")
    with secrets.transaction():
        saved = secrets.get(ref)
        try:
            latest = json.loads(saved) if saved else {"cookies": [], "origins": []}
        except (TypeError, ValueError) as exc:
            raise ValueError("Stored browser profile is not valid JSON") from exc
        merged, report = merge_storage_state(baseline, current, latest)
        secrets._set(ref, json.dumps(merged))
    return report


@dataclass
class BrowserSession:
    id: str
    job_id: str
    context: object
    page: object
    policy: AccessPolicy
    profile_id: str
    interval: float
    agent_id: str = "agent"
    principal_id: str = "operator"
    profile_ref: str = ""
    persist_profile: bool = False
    profile_baseline: dict = field(default_factory=lambda: {"cookies": [], "origins": []})
    profile_save_status: dict = field(default_factory=dict)
    mission: dict = field(default_factory=dict)
    epoch: int = 1
    control: str = "agent"
    human_id: str = "operator"
    frame_id: int = 0
    frame: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set = field(default_factory=set)
    downloads: list = field(default_factory=list)
    download_tasks: set = field(default_factory=set)
    observers: set = field(default_factory=set)
    observer_errors: list = field(default_factory=list)
    cdp: object = None
    closed: bool = False
    closing: bool = False
    elements: list = field(default_factory=list)
    last_status: int | None = None
    challenge_id: str | None = None
    challenge_origin: str | None = None
    challenge_url: str | None = None
    challenge_reservation: dict | None = None
    request_denials: list = field(default_factory=list)
    request_timings: list = field(default_factory=list)
    network_failures: list = field(default_factory=list)
    response_diagnostics: list = field(default_factory=list)
    browser_environment: dict | None = None
    resource_interval: float = 0.1


class BrowserManager:
    def __init__(self, settings, limiter: RateLimiter, on_download=None, on_event=None, store=None, secrets=None):
        self.settings, self.limiter = settings, limiter
        self.on_download, self.on_event, self.store = on_download, on_event, store
        self.secrets = secrets or SecretStore(settings.state_dir)
        self.sessions: dict[str, BrowserSession] = {}
        self.playwright = self.browser = None
        self.start_lock, self.profile_lock, self.create_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()

    async def _emit(self, job_id, kind, payload):
        from .provider_enrollment import is_setup, redact_setup
        for session in self.sessions.values():
            if session.job_id == job_id and is_setup(session):
                payload = redact_setup(session, payload, self.secrets)
        if self.on_event:
            result = self.on_event(job_id, kind, redact(payload))
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def _observer_failed(session, kind, exc):
        # Diagnostic sinks can fail after a worker lease or page has closed.
        # Preserve structural evidence only, never the exception body or URL.
        session.observer_errors.append({"kind": kind, "error_code": "diagnostic_callback_failed",
            "exception_type": type(exc).__name__, "at": datetime.now(timezone.utc).isoformat()})
        session.observer_errors[:] = session.observer_errors[-20:]

    def _launch_observer(self, session, kind, callback, *args):
        if session.closed:
            return
        task = asyncio.create_task(callback(*args))
        session.observers.add(task)

        def finished(done):
            session.observers.discard(done)
            if not done.cancelled():
                failure = done.exception()  # Consume failures even if no caller awaits this event.
                if failure is not None:
                    self._observer_failed(session, kind, failure)

        task.add_done_callback(finished)

    async def _emit_diagnostic(self, session, kind, payload):
        if session.closed:
            return
        try:
            await self._emit(session.job_id, kind, payload)
        except Exception as exc:
            # A logging failure must not turn denied network admission into an
            # allowed or permanently pending request. The denial remains recorded.
            self._observer_failed(session, kind, exc)

    @staticmethod
    async def _abort_request(session, request_route, code):
        try:
            await request_route.abort(code)
        except PlaywrightError:
            if not session.closed:
                raise

    async def start(self):
        async with self.start_lock:
            if self.browser:
                return
            installed = self.settings.state_dir / "browsers"
            if installed.exists():
                os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(installed))
            self.playwright = await async_playwright().start()
            environment = dict(os.environ)
            libraries = self.settings.state_dir / "browser-libs" / "usr" / "lib" / "x86_64-linux-gnu"
            if libraries.is_dir():
                environment["LD_LIBRARY_PATH"] = str(libraries) + (
                    os.pathsep + environment["LD_LIBRARY_PATH"] if environment.get("LD_LIBRARY_PATH") else "")
            options = {"headless": self.settings.browser_headless, "env": environment}
            if self.settings.browser_executable:
                options["executable_path"] = self.settings.browser_executable
            if self.settings.browser_proxy:
                options["proxy"] = {"server": self.settings.browser_proxy}
            try:
                self.browser = await self.playwright.chromium.launch(**options)
            except BaseException:
                await self.playwright.stop()
                self.playwright = None
                raise

    async def create(self, job_id: str, mission: dict, profile: dict | None = None, *, agent_id=None):
        async with self.create_lock:
            return await self._create(job_id, mission, profile, agent_id=agent_id)

    async def _create(self, job_id: str, mission: dict, profile: dict | None = None, *, agent_id=None):
        profile = profile or {}
        resource_interval = browser_resource_interval(mission, profile)
        companion = getattr(self, 'companion_hub', None)
        paired = companion.available(job_id) if companion and not profile.get('require_desktop') else None
        if paired:
            return await self._create_companion(job_id, mission, profile, agent_id, paired)
        if not profile.get('require_desktop') and (profile.get('require_companion') or companion and companion.selected(job_id)):
            raise AccessDenied('Use the existing Chrome companion session or reconnect it; server-browser fallback is disabled for this mission')
        backend = profile.get('browser_backend', getattr(self.settings, 'browser_backend', 'playwright'))
        if backend == 'desktop_chrome':
            return await self._create_desktop(job_id, mission, profile, agent_id)
        if backend != 'playwright':
            raise AccessDenied('Unknown browser backend')
        await self.start()
        sid, profile_id = uuid.uuid4().hex, str(profile.get("id", "public"))
        principal = str(profile.get("principal_id", "operator"))
        ref = "browser-profile:" + canonical_digest([principal, profile_id])
        persist = bool(profile.get("persist_session", profile_id != "public"))
        cap = int(profile.get("max_browser_sessions", max(4, self.settings.max_workers) if profile_id == "public" else 1))
        active = [item for item in self.sessions.values() if not item.closed and item.profile_ref == ref]
        if len(active) >= cap:
            raise AccessDenied("Access profile browser session limit reached")
        options = {"accept_downloads": True, "viewport": {"width": 1280, "height": 800}, "service_workers": "block"}
        baseline = {"cookies": [], "origins": []}
        if persist:
            async with self.profile_lock:
                saved = await asyncio.to_thread(self.secrets.get, ref)
            if saved:
                baseline = json.loads(saved)
                options["storage_state"] = deepcopy(baseline)
        if profile.get("proxy"):
            options["proxy"] = {"server": profile["proxy"]}
        context = await self.browser.new_context(**options)
        page = await context.new_page()
        session = BrowserSession(sid, job_id, context, page, AccessPolicy(mission, profile), profile_id,
            float(mission.get("limits", {}).get("origin_min_interval_seconds", 3)),
            agent_id=agent_id or f"agent:{sid}", principal_id=principal, profile_ref=ref,
            persist_profile=persist, profile_baseline=deepcopy(baseline), mission=mission, resource_interval=resource_interval)
        self.sessions[sid] = session
        if self.store:
            control = await asyncio.to_thread(self.store.acquire_control, job_id, sid, session.agent_id, "agent", None, 3600)
            session.epoch = control["epoch"]
            await asyncio.to_thread(self.store.record_observation, job_id,
                {"kind": "browser.created", "session_id": sid, "profile_id": profile_id, "agent_id": session.agent_id})

        async def route(request_route):
            request = request_route.request
            try:
                try:
                    frame=request.frame
                    while frame.parent_frame is not None:frame=frame.parent_frame
                    top_url=frame.url
                    is_top=request.is_navigation_request() and request.frame.parent_frame is None
                except PlaywrightError:
                    top_url='about:blank';is_top=True
                if is_top:
                    session.request_denials.clear()
                    session.network_failures.clear()
                    session.response_diagnostics.clear()
                await session.policy.check_browser_request(request.url,top_level_url=top_url,
                    is_top_level_navigation=is_top,resource_type=request.resource_type)
                lane = session.policy.browser_request_lane(request.url, top_level_url=top_url,
                    is_top_level_navigation=is_top, resource_type=request.resource_type)
                queued = time.monotonic()
                interval = session.resource_interval if lane == 'browser_resource' else session.interval
                await self.limiter.acquire(f"{session.profile_id}:{urlsplit(request.url).hostname}", interval, lane=lane)
                session.request_timings.append({'url': redact(request.url), 'resource_type': request.resource_type,
                    'lane': lane, 'queue_seconds': round(time.monotonic() - queued, 6),
                    'at': datetime.now(timezone.utc).isoformat()})
                session.request_timings[:] = session.request_timings[-100:]
                await request_route.continue_()
            except DNSResolutionFailed:
                try:
                    diagnostic = classify_network_observation(request.url, error_code="net::ERR_NAME_NOT_RESOLVED")
                    if not diagnostic["expected"]:
                        denial = {"url": diagnostic["evidence"]["url_origin"], "reason": "DNS resolution failed",
                                  "url_sha256": diagnostic["evidence"]["url_sha256"], "resource_type": request.resource_type,
                                  "diagnostic": diagnostic, "at": datetime.now(timezone.utc).isoformat()}
                        session.request_denials.append(denial)
                        session.request_denials[:] = session.request_denials[-20:]
                        await self._emit_diagnostic(session, "request_blocked", denial)
                finally:
                    # Preserve the transport failure even when diagnostics fail.
                    await self._abort_request(session, request_route, "namenotresolved")
            except AccessDenied as exc:
                try:
                    denial={"url":redact(request.url),"reason":str(exc),"resource_type":request.resource_type,
                            "at":datetime.now(timezone.utc).isoformat()}
                    session.request_denials.append(denial);session.request_denials[:]=session.request_denials[-20:]
                    await self._emit_diagnostic(session, "request_blocked", denial)
                finally:
                    await self._abort_request(session, request_route, "blockedbyclient")

        await context.route("**/*", route)

        response_chains = {}

        async def response_seen(response):
            chain=[];request=response.request
            while request is not None:
                chain.append(request.url);request=request.redirected_from
            if chain:response_chains[response.url]=list(reversed(chain))
            if len(response_chains)>1000:response_chains.pop(next(iter(response_chains)))
            if response.request.is_navigation_request() and response.frame == session.page.main_frame:
                session.last_status = response.status
            if response.status == 429:
                try:
                    delay = float((await response.all_headers()).get("retry-after", "30"))
                except ValueError:
                    delay = 30
                await self.limiter.penalize(f"{profile_id}:{urlsplit(response.url).hostname}", min(max(delay, 0), 3600))
            if response.status in (401, 403, 429):
                top = urlsplit(session.page.url)
                diagnostic = classify_network_observation(response.url, status=response.status,
                    authorized_top_origin=f"{top.scheme}://{top.netloc}")
                try:
                    headers = await response.all_headers()
                except PlaywrightError:
                    # Closing or navigating the page can invalidate header access;
                    # the observed status remains useful without optional headers.
                    headers = {}
                value = {"url_origin": diagnostic["evidence"]["url_origin"],
                         "url_sha256": diagnostic["evidence"]["url_sha256"], "status": response.status,
                         "resource_type": response.request.resource_type,
                         "cf_mitigated": "challenge" if headers.get("cf-mitigated", "").lower() == "challenge" else None,
                         "diagnostic": diagnostic, "at": datetime.now(timezone.utc).isoformat()}
                session.response_diagnostics.append(value)
                session.response_diagnostics[:] = session.response_diagnostics[-30:]
                await self._emit(job_id, "browser.http_status", value)

        async def request_failed(request):
            diagnostic = classify_network_observation(request.url, error_code=request.failure)
            code = diagnostic["evidence"]["observed_error_code"]
            value = {"url_origin": diagnostic["evidence"]["url_origin"],
                     "url_sha256": diagnostic["evidence"]["url_sha256"], "resource_type": request.resource_type,
                     "error_code": "net::" + code if code and code.startswith("ERR_") else code,
                     "diagnostic": diagnostic, "at": datetime.now(timezone.utc).isoformat()}
            session.network_failures.append(value)
            session.network_failures[:] = session.network_failures[-30:]
            await self._emit(job_id, "browser.network_failure", value)

        context.on("requestfailed", lambda request: self._launch_observer(session, "requestfailed", request_failed, request))

        async def download(item):
            from .provider_enrollment import block_setup_download
            if await block_setup_download(self, session, item):
                return
            path = self.settings.state_dir / "staging" / uuid.uuid4().hex
            path.parent.mkdir(parents=True, exist_ok=True)
            value = {"filename": item.suggested_filename, "url": item.url, "session_id": sid,
                     "observed_page_url": session.page.url, "status": "pending"}
            try:
                chain=response_chains.get(item.url,[item.url])
                policy=AccessPolicy(mission,profile,operation='download')
                try:
                    for target in dict.fromkeys([*chain,item.url]):await policy.check(target)
                except AccessDenied:
                    await item.cancel()
                    raise
                await item.save_as(path)
                failure = await item.failure()
                if failure:
                    raise AccessDenied("Browser download failed: " + failure)
                maximum = int(mission.get("limits", {}).get("max_artifact_bytes", 256 * 1024 * 1024))
                if path.stat().st_size > maximum:
                    raise AccessDenied("Browser download exceeds artifact byte limit")
                chain=response_chains.get(item.url,[item.url])
                value.update(path=str(path), bytes=path.stat().st_size, status="staged",
                    requested_source_url=chain[0],redirect_chain=chain)
                session.downloads.append(value)
                locator_ref = "browser-download:" + canonical_digest([job_id, sid, str(path)])
                async with self.profile_lock:
                    await asyncio.to_thread(self.secrets.set, locator_ref, json.dumps({"url": item.url}))
                if self.store:
                    await asyncio.to_thread(self.store.record_observation, job_id,
                        {"kind": "browser.download", **redact(value), "locator_ref": locator_ref})
                await self._emit(job_id, "browser.download", value)
                if self.on_download:
                    result = self.on_download(session, value)
                    if inspect.isawaitable(result):
                        await result
            except Exception as exc:
                path.unlink(missing_ok=True)
                value.update(status="failed", error=type(exc).__name__, message=str(exc)[:500])
                session.downloads.append(value)
                await self._emit(job_id, "browser.download_failed", value)

        def launch_download(item):
            task = asyncio.create_task(download(item))
            session.download_tasks.add(task)
            task.add_done_callback(session.download_tasks.discard)

        def attach(new_page):
            new_page.on("download", launch_download)
            new_page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
            new_page.on("response", lambda response: self._launch_observer(session, "response", response_seen, response))

        attach(page)
        context.on("page", attach)
        await self._connect_frames(session)
        return session

    async def _create_desktop(self, job_id, mission, profile, agent_id):
        from .desktop import DesktopRuntime
        from .desktop_browser import create_context
        if getattr(self.settings, 'browser_proxy', None):
            raise AccessDenied('Native desktop does not support a coordinator proxy override')
        profile_id = str(profile.get('id', 'public'))
        principal = str(profile.get('principal_id', 'operator'))
        cap = int(profile.get('max_browser_sessions', 4 if profile_id == 'public' else 1))
        active = [s for s in self.sessions.values() if not s.closed and s.profile_id == profile_id and s.principal_id == principal]
        if len(active) >= cap:
            raise AccessDenied('Access profile browser session limit reached')
        runtime = getattr(self, 'desktop_runtime', None)
        if runtime is None:
            runtime = self.desktop_runtime = DesktopRuntime(self.settings.state_dir / 'desktop',
                image=getattr(self.settings, 'desktop_image', 'ore-desktop:0.2.0rc1'),
                host_state_dir=getattr(self.settings, 'desktop_host_state_dir', None), width=1280, height=800,
                resource_mode=getattr(self.settings, 'desktop_resource_mode', 'cgroup'),
                memory=getattr(self.settings, 'desktop_memory', '4g'))
        sid = uuid.uuid4().hex
        context = await create_context(runtime, sid, mission, profile)
        page = context.pages[0]
        session = BrowserSession(sid, job_id, context, page, AccessPolicy(mission, profile), profile_id,
            float(mission.get('limits', {}).get('origin_min_interval_seconds', 3)),
            agent_id=agent_id or f'agent:{sid}', principal_id=principal, mission=mission)
        page.session = session
        session.profile_save_status = {'saved': False, 'status': 'desktop_session_profile',
            'scope': 'isolated_desktop', 'cookies_exported': False}
        try:
            if self.store:
                control = await asyncio.to_thread(self.store.acquire_control, job_id, sid, session.agent_id, 'agent', None, 3600)
                session.epoch = control['epoch']
            self.sessions[sid] = session
        except BaseException:
            await context.close()
            raise
        async def frames():
            while not session.closed:
                native = getattr(runtime, 'sessions', {}).get(sid)
                if hasattr(runtime, 'sessions') and (native is None or native.closed):
                    session.closed = True
                    context.closed = True
                    await self._emit(job_id, 'browser_handoff', {'session_id': sid,
                        'task_id': session.agent_id.removeprefix('task:') if session.agent_id.startswith('task:') else None,
                        'url': page.url, 'reason': 'The ORE desktop ended. Recreate it to continue.'})
                    await self._emit(job_id, 'browser_session_lost', {'session_id': sid,
                        'reason': 'desktop_runtime_ended'})
                    return
                if hasattr(runtime, 'renew_lease') and native is not None:
                    # The coordinator owns this renewable session lease. The
                    # challenge deadline remains independent and nonrenewable.
                    runtime.renew_lease(sid)
                if session.subscribers and not session.lock.locked():
                    async with session.lock:
                        await page.screenshot()
                await asyncio.sleep(1)
        self._launch_observer(session, 'desktop_frames', frames)
        await self._emit(job_id, 'browser.created', {'session_id': sid, 'transport': 'desktop_chrome'})
        return session

    async def _create_companion(self, job_id, mission, profile, agent_id, paired):
        from .companion import CompanionPage, CompanionContext
        cap=int(profile.get('max_browser_sessions',4 if profile.get('id','public')=='public' else 1))
        active=[s for s in self.sessions.values() if not s.closed and s.profile_id==profile.get('id','public') and s.principal_id==profile.get('principal_id','operator')]
        if len(active)>=cap:raise AccessDenied('Access profile browser session limit reached')
        sid = uuid.uuid4().hex
        page = CompanionPage(self.companion_hub, paired)
        session = BrowserSession(sid, job_id, CompanionContext(page), page, AccessPolicy(mission, profile),
            str(profile.get('id', 'public')), float(mission.get('limits', {}).get('origin_min_interval_seconds', 3)),
            agent_id=agent_id or f'agent:{sid}', principal_id=profile.get('principal_id', 'operator'), mission=mission)
        page.session = session
        if self.store:
            control = await asyncio.to_thread(self.store.acquire_control, job_id, sid, session.agent_id, 'agent', None, 3600)
            session.epoch = control['epoch']
        self.sessions[sid] = session
        paired['session_id'] = sid
        async def frames():
            while not session.closed:
                if session.subscribers and not session.lock.locked():
                    try:
                        async with session.lock:
                            await page.screenshot()
                    except AccessDenied:
                        pass
                await asyncio.sleep(1)
        self._launch_observer(session, 'companion_frames', frames)
        await self._emit(job_id, 'browser.created', {'session_id': sid, 'transport': 'chrome_companion'})
        return session

    async def _connect_frames(self, session):
        if session.cdp:
            try:
                await session.cdp.detach()
            except Exception:
                pass
        session.cdp = await session.context.new_cdp_session(session.page)

        async def frame(params):
            if session.closed:
                return
            session.frame_id += 1
            session.frame = {"type": "frame", "data": params["data"], "width": 1280, "height": 800,
                "frame_id": session.frame_id, "epoch": session.epoch, "control": session.control,
                "scale": params.get("metadata", {}).get("pageScaleFactor", 1)}
            for queue in list(session.subscribers):
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(session.frame)
            try:
                await session.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                pass

        session.cdp.on("Page.screencastFrame", lambda params: asyncio.create_task(frame(params)))
        await session.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 65,
            "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 1})

    def get(self, sid):
        session = self.sessions.get(sid)
        if not session or session.closed:
            raise AccessDenied("Browser session is unavailable; recreate and reauthenticate")
        return session

    async def summary(self, session):
        try:
            title = await session.page.title()
        except Exception:
            title = ""
        value = {"id": session.id, "session_id": session.id, "job_id": session.job_id,
                "url": session.page.url, "title": title, "control": session.control,
                "epoch": session.epoch, "width": 1280, "height": 800,
                "challenge_id": session.challenge_id, "termination_unconfirmed": session.closing and not session.closed,
                "transport": "desktop_chrome" if getattr(session.context,'desktop',False) else "chrome_companion" if getattr(session.context,'companion',False) else "playwright",
                "download_via_session": bool(getattr(session.context,'companion',False) or getattr(session.context,'desktop',False)),
                "profile_persistence": session.profile_save_status,
                "browser_environment": session.browser_environment,
                "network_diagnostics":{"blocked_requests":list(session.request_denials),"count":len(session.request_denials),
                    "request_failures": list(session.network_failures), "responses": list(session.response_diagnostics),
                    "callback_errors": list(session.observer_errors),
                    "request_grants":list(session.request_timings),"browser_resource_min_interval_seconds":session.resource_interval}}
        from .provider_enrollment import is_setup, redact_setup, safe_setup_url
        if is_setup(session):
            value.update(title="Provider setup", url=safe_setup_url(session.page.url), network_diagnostics={})
            value = redact_setup(session, value, self.secrets)
        return value

    async def list(self):
        return [await self.summary(session) for session in self.sessions.values() if not session.closed]

    async def _authorize(self, session, owner, epoch=None, owner_id=None):
        if session.closing:raise AccessDenied('Browser termination is pending confirmation')
        if session.control != owner:
            raise AccessDenied(f"Browser control belongs to {session.control}")
        if epoch is not None and epoch != session.epoch:
            raise AccessDenied("Stale browser control epoch")
        identity = session.agent_id if owner == "agent" else session.human_id
        if owner_id is not None and owner_id != identity:
            raise AccessDenied("Browser belongs to a different actor")
        if self.store:
            try:
                await asyncio.to_thread(self.store.validate_control, session.job_id, session.id, identity, session.epoch)
            except ControlConflict as exc:
                raise AccessDenied(str(exc)) from exc

    async def _publish_control(self, session):
        if session.frame:
            session.frame_id += 1
            session.frame = {**session.frame, "epoch": session.epoch, "control": session.control,
                             "frame_id": session.frame_id}
            for queue in list(session.subscribers):
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(dict(session.frame))

    async def _takeover_locked(self, session, user_id="operator"):
        if session.control == "human" and session.human_id != user_id:
            raise AccessDenied("Another human owns this browser")
        if self.store:
            request = await asyncio.to_thread(self.store.request_control, session.job_id, session.id, user_id)
            result = await asyncio.to_thread(self.store.acquire_control, session.job_id, session.id,
                                            user_id, "user", request["epoch"], 3600)
            session.epoch = result["epoch"]
        else:
            session.epoch += 1
        session.control, session.human_id = "human", user_id
        session.elements = []
        await self._publish_control(session)
        await self._emit(session.job_id, "browser_handoff", {
            "session_id": session.id, "epoch": session.epoch, "url": session.page.url,
            "task_id": session.agent_id.removeprefix("task:") if session.agent_id.startswith("task:") else None,
            "challenge_id": session.challenge_id, "access_profile": session.profile_id})
        return await self.summary(session)

    async def takeover(self, sid, user_id="operator"):
        session = self.get(sid)
        async with session.lock:
            return await self._takeover_locked(session, user_id)

    async def resume(self, sid, user_id="operator"):
        session = self.get(sid)
        async with session.lock:
            if session.control != "human" or session.human_id != user_id:
                raise AccessDenied("Only the current human controller can return the browser")
            if session.challenge_id and self.store:
                recovered, evidence = await self._target_recovered(session)
                if recovered and not session.challenge_reservation:
                    await asyncio.to_thread(self.store.resolve_challenge, session.challenge_id, evidence)
            await self.save_profile(session)
            if self.store:
                released = await asyncio.to_thread(self.store.release_control, session.job_id, sid, user_id, session.epoch)
                acquired = await asyncio.to_thread(self.store.acquire_control, session.job_id, sid,
                    session.agent_id, "agent", released["epoch"], 3600)
                session.epoch = acquired["epoch"]
            else:
                session.epoch += 1
            session.control, session.elements = "agent", []
            await self._publish_control(session)
            await self._emit(session.job_id, "browser_resumed", {"session_id": sid, "epoch": session.epoch})
            return await self.summary(session)

    async def save_profile(self, session):
        if getattr(session.context, 'desktop', False):
            return session.profile_save_status
        if not session.persist_profile:
            return
        state = await session.context.storage_state()
        async with self.profile_lock:
            remote_merge = getattr(self.secrets, "merge_browser_profile", None)
            if callable(remote_merge):
                report = await asyncio.to_thread(remote_merge, session.profile_ref, session.profile_baseline, state)
            else:
                report = await asyncio.to_thread(merge_browser_profile, self.secrets, session.profile_ref,
                                                 session.profile_baseline, state)
            # The baseline is what this context observed, not another context's
            # merged state. Otherwise its next save could resurrect deleted keys.
            session.profile_baseline = deepcopy(state)
            session.profile_save_status = report
        if report["conflict_count"]:
            await self._emit(session.job_id, "browser.profile_conflict", {"session_id": session.id, **report})
        return report

    async def _challenge_visible(self, session):
        if getattr(session.context, 'desktop', False):
            from .desktop_browser import challenge_visible
            return await challenge_visible(session)
        if session.page.url == "about:blank":
            return False
        selectors = '[data-ore-challenge="true"]:visible,#challenge-form:visible,#cf-challenge-running:visible,iframe[src*="challenges.cloudflare.com"]:visible'
        if await session.page.locator(selectors).count():
            return True
        title = (await session.page.title()).strip().lower()
        return title in ("just a moment...", "verify you are human", "checking your browser", "attention required! | cloudflare")

    async def _target_recovered(self, session):
        if getattr(session.context, 'desktop', False):
            from .desktop_browser import target_recovered
            return await target_recovered(session)
        if await self._challenge_visible(session):
            return False, {"reason": "challenge_still_visible"}
        origin = f"{urlsplit(session.page.url).scheme}://{urlsplit(session.page.url).netloc}"
        if session.challenge_origin and origin != session.challenge_origin:
            return False, {"reason": "different_origin_is_not_resolution"}
        if session.challenge_url:
            expected_url, current_url = urlsplit(session.challenge_url), urlsplit(session.page.url)
            if expected_url.path != current_url.path and not session.policy.profile.get("challenge_success_selector"):
                return False, {"reason": "different_page_requires_a_configured_success_check"}
        if await session.page.locator('input[type="password"]:visible').count():
            return False, {"reason": "authentication_required"}
        if session.last_status is not None and session.last_status >= 400:
            return False, {"reason": "http_error", "status": session.last_status}
        selector = session.policy.profile.get("challenge_success_selector")
        text = (await session.page.locator("body").inner_text()) if session.page.url != "about:blank" else ""
        present = bool(await session.page.locator(selector).count()) if selector else len(text.strip()) >= 80
        return present, {"url": redact(session.page.url), "status": session.last_status,
                         "success_selector": selector, "substantive_content_observed": present}

    async def _challenge_evidence(self, session, observed=None):
        import hashlib
        import re
        observation = observed or (getattr(session.page,'latest',{}) if getattr(session.context,'desktop',False) else {})
        text = str(observation.get('text', ''))
        accepted = False
        if not getattr(session.context, 'desktop', False):
            try:
                async with asyncio.timeout(1):
                    if not text: text = await session.page.locator('body').inner_text()
                    accepted = bool(await session.page.locator('input[type="checkbox"]:checked, [role="checkbox"][aria-checked="true"]').count())
            except (PlaywrightError, TimeoutError): pass
        phase = 'verification_accepted' if accepted else 'verification_visible'
        normalized = re.sub(r'\s+', ' ', re.sub(r'\b[0-9a-fA-F]{8,}\b|\d+', '', text.casefold()))[:4000]
        evidence = {'phase': phase, 'verification_accepted': accepted,
            'fingerprint': hashlib.sha256((urlsplit(session.page.url).path + normalized + phase).encode()).hexdigest(),
            'environment': 'desktop_chrome' if getattr(session.context, 'desktop', False) else 'playwright'}
        if session.last_status == 429: evidence['reason'] = 'rate_limited'
        if (session.browser_environment or {}).get('code') == 'debug_automation_detected': evidence['reason'] = 'environment_incompatible'
        return evidence

    async def _track_challenge(self, session, result, owner='agent'):
        if owner != 'agent' or not self.store or session.mission.get('on_challenge', {}).get('policy_version') != 2:
            return result
        if not result.get('challenge_detected'): return result
        origin = f"{urlsplit(session.page.url).scheme}://{urlsplit(session.page.url).netloc}"
        # Start the elapsed clock before any extra DOM probing or screenshot.
        evidence = {'phase':'verification_visible',
            'environment':'desktop_chrome' if getattr(session.context,'desktop',False) else 'playwright'}
        if session.last_status == 429:evidence['reason']='rate_limited'
        if (session.browser_environment or {}).get('code') == 'debug_automation_detected':evidence['reason']='environment_incompatible'
        episode = await asyncio.to_thread(self.store.observe_challenge, session.job_id, origin,
            session.profile_id + ':' + session.principal_id, evidence)
        if not episode: return result
        session.challenge_id, session.challenge_origin, session.challenge_url = episode['id'], origin, session.page.url
        result['challenge'] = {key:episode.get(key) for key in ('clock','state','attempts','max_attempts','elapsed_seconds','remaining_seconds','deadline_at','hard_deadline_at')}
        if episode.get('state') == 'awaiting_user':
            await self._takeover_locked(session)
            result.update(await self.summary(session))
        return result

    async def _challenge_deadline_expired(self, session):
        if not self.store or not session.challenge_id: return False
        episode = await asyncio.to_thread(self.store.get_challenge, session.challenge_id)
        if not episode or episode.get('clock') != 'elapsed' or episode.get('state') == 'resolved': return False
        if datetime.fromisoformat(episode['deadline_at']) <= datetime.now(timezone.utc):
            await asyncio.to_thread(self.store.adapt_challenge, episode['id'])
            await self._takeover_locked(session)
            return True
        return False

    async def challenge(self, sid):
        session = self.get(sid)
        async with session.lock:
            await self._authorize(session, "agent")
            if not await self._challenge_visible(session):
                return {"allowed": False, "reason": "no_challenge_observed", **await self.summary(session)}
            if not self.store:
                raise AccessDenied("Durable challenge accounting requires a Store")
            origin = f"{urlsplit(session.page.url).scheme}://{urlsplit(session.page.url).netloc}"
            auth_context = session.profile_id + ":" + session.principal_id
            existing = session.challenge_reservation
            if existing:
                current = await asyncio.to_thread(self.store.get_challenge, existing["id"])
                if (current and current.get("token") == existing.get("token")
                        and current.get("origin") == origin and current.get("auth_context") == auth_context
                        and current.get("state") == "attempting" and current.get("expires_at")
                        and datetime.fromisoformat(current["expires_at"]) > datetime.now(timezone.utc)):
                    session.challenge_reservation = current
                    return {**await self.summary(session), **{key: value for key, value in current.items() if key != "token"},
                            "allowed": True, "reason": "attempt_already_reserved"}
                # A stale local reference cannot take ownership of another reservation.
                session.challenge_reservation = None
            config = session.mission.get("on_challenge", {})
            if config.get('policy_version') == 2:
                tracked = await self._track_challenge(session, {'challenge_detected': True})
                if session.control != 'agent':
                    return {**tracked, **await self.summary(session), 'allowed': False, 'reason': 'budget_exhausted'}
            reservation = await asyncio.to_thread(self.store.reserve_challenge, session.job_id, origin,
                auth_context, int(config.get("max_attempts_per_episode", 3)), float(config.get("max_active_seconds", 120)))
            session.challenge_id, session.challenge_origin, session.challenge_url = reservation["id"], origin, session.page.url
            if reservation["allowed"]:
                session.challenge_reservation = reservation
            elif reservation.get("reason") == "budget_exhausted":
                await self._takeover_locked(session)
                if not session.agent_id.startswith("task:"):
                    await asyncio.to_thread(self.store.update_job, session.job_id, status="awaiting_user")
            result = {**await self.summary(session), **{key: value for key, value in reservation.items() if key != "token"}}
            if reservation.get("reason") == "attempt_in_flight":
                result.update(code="challenge_contention", retryable=True,
                    retry_after=max(0.0, (datetime.fromisoformat(reservation["expires_at"]) - datetime.now(timezone.utc)).total_seconds()))
            return result

    async def _settle_challenge_recovery(self, session, reservation):
        """Observe an ordinary action's asynchronous result within its existing budget."""
        from .desktop import DesktopError
        try:
            native = bool(getattr(session.context, "desktop", False))
            seconds = float(session.mission.get("on_challenge", {}).get("recovery_settle_seconds", 30 if native else 5))
        except (TypeError, ValueError):
            seconds = 5.0
        if not math.isfinite(seconds):
            seconds = 5.0
        remaining = max(0.0, (datetime.fromisoformat(reservation["expires_at"]) - datetime.now(timezone.utc)).total_seconds())
        deadline = time.monotonic() + min(max(0.0, min(seconds, 30.0 if getattr(session.context, "desktop", False) else 10.0)), remaining)
        evidence = {"reason": "recovery_observation_deadline"}
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                async with asyncio.timeout(remaining):
                    recovered, evidence = await self._target_recovered(session)
                if recovered:
                    return True, evidence
            except (PlaywrightError, TimeoutError):
                # Navigation can replace the DOM mid-check. It must not skip
                # finish_challenge or leave a reservation permanently in flight.
                evidence = {"reason": "page_transition_during_recovery_check"}
            except (AccessDenied, DesktopError) as exc:
                if not native:
                    raise
                # An unreadable native URL/window is failed recovery evidence,
                # never permission to bypass access checks or leave the token live.
                evidence = {"reason": "native_recovery_observation_unavailable", "code": type(exc).__name__}
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(0.1, remaining))
        return False, evidence

    async def verify_challenge(self, sid):
        session = self.get(sid)
        async with session.lock:
            recovered, evidence = await self._target_recovered(session)
            if recovered and session.challenge_reservation:
                record = session.challenge_reservation
                await asyncio.to_thread(self.store.finish_challenge, record["id"], record["token"], 0, True, evidence)
                session.challenge_reservation = None
            elif recovered and session.challenge_id and self.store:
                await asyncio.to_thread(self.store.resolve_challenge, session.challenge_id, evidence)
            return {"resolved": recovered, "evidence": evidence}

    async def observe(self, sid, *, owner="agent", screenshot=True, owner_id=None):
        session = self.get(sid)
        async with session.lock:
            return await self._observe_unlocked(sid, owner=owner, screenshot=screenshot, owner_id=owner_id)

    async def _observe_unlocked(self, sid, *, owner="agent", screenshot=True, owner_id=None):
        session = self.get(sid)
        await self._authorize(session, owner, owner_id=owner_id)
        page = session.page
        from .provider_enrollment import is_setup, setup_browser_observation
        if is_setup(session):
            return await setup_browser_observation(self, session)
        if getattr(session.context, 'desktop', False):
            from .desktop_browser import observe, collect_downloads
            observed = await observe(session)
            staged = await collect_downloads(session, self.settings.state_dir / 'staging', self.store)
            for item in staged:
                await self._emit(session.job_id, 'browser.download', item)
            session.elements = []
            session.last_status = None
            result = {**await self.summary(session), **observed,
                'downloads': redact(list(session.downloads)), 'tabs': []}
            if self.store:
                await asyncio.to_thread(self.store.record_observation, session.job_id,
                    {'kind': 'browser.page', 'session_id': sid, 'source_url': redact(page.url),
                     'url': redact(page.url), 'title': result.get('title', ''),
                     'text_excerpt': result.get('text', '')[:4000], 'elements': [],
                     'challenge_detected': result['challenge_detected'],
                     'observation_kind': 'desktop_screenshot_ocr', 'http_status': None})
            result = await self._track_challenge(session, result, owner)
            if session.control != owner: return result
            if screenshot:
                png = page.latest['png']
                await page.publish_frame(png)
                result['image_url'] = 'data:image/png;base64,' + base64.b64encode(png).decode()
            return result
        text = (await page.locator("body").inner_text(timeout=15000))[:30000] if page.url != "about:blank" else ""
        elements = await page.locator('a,button,input,textarea,select,[role="button"]').evaluate_all('''els => els.map((e,i)=>({
            index:i,tag:e.tagName.toLowerCase(),text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').slice(0,200),
            type:e.getAttribute('type'),name:e.getAttribute('name'),href:e.href||null,
            visible:!!(e.getClientRects().length)
        })).filter(e=>e.visible).slice(0,180)''') if page.url != "about:blank" else []
        session.elements = elements
        # Inspect, never override, the browser's own state. Keep unsupported
        # automation separate from proof that a particular publisher blocked it.
        if "cloudflare" in text.casefold() or urlsplit(page.url).hostname == "debug.challenges.cloudflare.com":
            try:
                webdriver = await page.evaluate("() => navigator.webdriver")
                session.browser_environment = classify_runtime_diagnostic(url=page.url, page_text=text,
                    navigator_webdriver=webdriver)
            except PlaywrightError:
                session.browser_environment = None
        else:
            session.browser_environment = None
        result = {**await self.summary(session), "text": text, "elements": elements,
            "downloads": redact(list(session.downloads)), "challenge_detected": await self._challenge_visible(session),
            "tabs": [{"index": i, "url": redact(item.url)} for i, item in enumerate(session.context.pages)]}
        if self.store:
            await asyncio.to_thread(self.store.record_observation, session.job_id,
                {"kind": "browser.page", "session_id": sid, "source_url": redact(page.url),
                 "url": redact(page.url), "title": result["title"], "text_excerpt": text[:4000],
                 "elements": redact(elements), "challenge_detected": result["challenge_detected"],
                 "browser_environment": session.browser_environment})
        result = await self._track_challenge(session, result, owner)
        if session.control != owner: return result
        if screenshot:
            try:
                result["image_url"] = "data:image/png;base64," + base64.b64encode(
                    await page.screenshot(mask=[page.locator('input[type="password"]')], timeout=15000)).decode()
            except Exception as exc:
                # A slow font/asset must not discard valid DOM evidence. Never replace
                # a masked capture with an unmasked CDP screenshot for model input.
                result["screenshot_error"] = {"code": type(exc).__name__, "message": str(exc)[:500]}
                result["needs_screenshot"] = True
        return result

    async def action(self, sid, action: str, args: dict, *, owner="agent", owner_id=None):
        session = self.get(sid)
        async with session.lock:
            initial_navigation = owner == "agent" and action == "navigate" and session.page.url == "about:blank"
            if "epoch" not in args and not initial_navigation:
                raise AccessDenied("Browser input requires the epoch from the latest observation")
            await self._authorize(session, owner, args.get("epoch"), owner_id)
            if owner == "human" and args.get("frame_id", session.frame_id) < session.frame_id - 20:
                raise AccessDenied("Screen changed; wait for a current frame")
            if owner == 'agent' and await self._challenge_deadline_expired(session):
                return await self.summary(session)
            passive = action == "wait"
            visible_challenge = owner == "agent" and not passive and await self._challenge_visible(session)
            if visible_challenge and not session.challenge_reservation:
                raise AccessDenied("Challenge observed: reserve a bounded attempt or hand control to the user first")
            if self.store and owner == "agent":
                job = await asyncio.to_thread(self.store.get_job, session.job_id)
                maximum = int(session.mission.get("limits", {}).get("max_browser_actions", 1000))
                budget = await asyncio.to_thread(self.store.reserve_budget,
                    f"{session.job_id}:{job['revision']}:{job['generation']}", "browser_actions", 1, maximum)
                if not budget["allowed"]:
                    raise AccessDenied("Shared browser action budget exhausted")
            page, target = session.page, args.get("target")
            if getattr(session.context, 'desktop', False) and (target is not None or args.get('selector')):
                raise AccessDenied('Desktop Chrome uses screenshot coordinates; DOM targets and selectors are unavailable')
            if isinstance(target, int):
                if not any(item["index"] == target for item in session.elements):
                    raise AccessDenied("Target index was not present in the latest observation")
                locator = page.locator('a,button,input,textarea,select,[role="button"]').nth(target)
            else:
                locator = page.locator(args["selector"]) if args.get("selector") else None
            started, reservation = time.monotonic(), None if passive else session.challenge_reservation
            timeout = 90.0
            if reservation:
                timeout = max(0.0, min(timeout, (datetime.fromisoformat(reservation["expires_at"]) - datetime.now(timezone.utc)).total_seconds()))
            try:
                async with asyncio.timeout(timeout):
                    if action == "navigate":
                        policy = session.policy
                        if owner == 'human' and (getattr(session.context, 'companion', False) or getattr(session.context, 'desktop', False)):
                            from .companion import manual_policy
                            policy = manual_policy(session)
                        await policy.check(args["url"])
                        if getattr(session.context, 'desktop', False):
                            await self.limiter.acquire(f'{session.profile_id}:{urlsplit(args["url"]).hostname}', session.interval)
                        try:
                            response = await page.goto(args["url"], wait_until="domcontentloaded", timeout=60000)
                            session.last_status = response.status if response else None
                        except Exception as exc:
                            if "Download is starting" not in str(exc):
                                raise
                        await page.wait_for_timeout(150)
                    elif action == "click":
                        if locator:
                            await locator.click(timeout=15000)
                        else:
                            x, y = float(args["x"]), float(args["y"])
                            if not (0 <= x <= 1280 and 0 <= y <= 800):
                                raise AccessDenied("Click coordinates are outside the browser viewport")
                            await page.mouse.click(x, y)
                    elif action in ("type", "text"):
                        value = str(args.get("text", ""))
                        if len(value) > 10000:
                            raise AccessDenied("Input text exceeds per-action limit")
                        if locator:
                            await locator.fill(value, timeout=15000)
                        else:
                            await page.keyboard.insert_text(value)
                    elif action == "key":
                        await page.keyboard.press(args["key"])
                    elif action == "scroll":
                        await page.mouse.wheel(max(-10000, min(float(args.get("deltaX", 0)), 10000)),
                                              max(-10000, min(float(args.get("deltaY", 600)), 10000)))
                    elif action == "wait":
                        await page.wait_for_timeout(max(0, min(float(args.get("seconds", 1)), 10)) * 1000)
                    elif action == "tab":
                        index = int(args["index"])
                        if index < 0 or index >= len(session.context.pages):
                            raise AccessDenied("Unknown browser tab")
                        session.page = session.context.pages[index]
                        await session.page.bring_to_front()
                        if not getattr(session.context, 'companion', False) and not getattr(session.context, 'desktop', False):
                            await self._connect_frames(session)
                    elif action == "back":
                        await page.go_back(wait_until="domcontentloaded")
                    else:
                        raise AccessDenied(f"Unknown browser action: {action}")
                    if session.download_tasks:
                        await asyncio.wait_for(asyncio.gather(*list(session.download_tasks)), timeout=90)
            except TimeoutError as exc:
                raise AccessDenied("Browser action timed out or its active challenge budget expired") from exc
            finally:
                if reservation and owner == "agent":
                    recovered, evidence = await self._settle_challenge_recovery(session, reservation)
                    if not recovered and reservation.get('clock') == 'elapsed':
                        remaining = max(0, (datetime.fromisoformat(reservation['expires_at']) - datetime.now(timezone.utc)).total_seconds())
                        if remaining > 0:
                            try:
                                async with asyncio.timeout(min(remaining, 1)):
                                    evidence = {**(evidence or {}), **await self._challenge_evidence(session)}
                            except TimeoutError: pass
                    result = await asyncio.to_thread(self.store.finish_challenge, reservation["id"],
                        reservation["token"], time.monotonic() - started, recovered, evidence if recovered or reservation.get('clock') == 'elapsed' else None)
                    session.challenge_reservation = None
                    if result["state"] == "awaiting_user":
                        await self._takeover_locked(session)
                        if not session.agent_id.startswith("task:"):
                            await asyncio.to_thread(self.store.update_job, session.job_id, status="awaiting_user")
                await self._emit(session.job_id, "browser.action", {"session_id": sid, "action": action,
                    "owner": owner, "epoch": session.epoch, "elapsed_seconds": time.monotonic() - started})
            if owner == "human" or session.control != "agent":
                return await self.summary(session)
            return await self._observe_unlocked(sid, screenshot=args.get("screenshot", True), owner_id=owner_id)

    async def close_session(self, sid):
        session = self.get(sid)
        async with session.lock:
            if getattr(session.context,'desktop',False):
                session.closing=True
                pending=list(session.observers)
                for task in pending:task.cancel()
                if pending:await asyncio.gather(*pending,return_exceptions=True)
                session.observers.clear()
                await session.context.close()
                session.closed=True
                if self.store:
                    identity=session.agent_id if session.control=='agent' else session.human_id
                    try:await asyncio.to_thread(self.store.release_control,session.job_id,sid,identity,session.epoch)
                    except ControlConflict:pass
                return
            try:
                if session.download_tasks:
                    await asyncio.gather(*list(session.download_tasks), return_exceptions=True)
                await self.save_profile(session)
                if self.store:
                    identity = session.agent_id if session.control == "agent" else session.human_id
                    try:
                        await asyncio.to_thread(self.store.release_control, session.job_id, sid, identity, session.epoch)
                    except ControlConflict:
                        pass
            finally:
                # Stop accepting diagnostic events before closing the page; cancel
                # and retrieve existing tasks even if profile persistence failed.
                session.closed = True
                pending = list(session.observers)
                for task in pending:
                    task.cancel()
                try:
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    session.observers.clear()
                finally:
                    await session.context.close()

    async def close(self):
        errors = []
        for session in list(self.sessions.values()):
            if not session.closed:
                try:
                    await self.close_session(session.id)
                except Exception as exc:
                    if getattr(session.context,'desktop',False):errors.append(exc)
                    else:session.closed = True
        browser, playwright = self.browser, self.playwright
        self.browser = self.playwright = None
        desktop = getattr(self, "desktop_runtime", None)
        for target, method in ((browser, "close"), (playwright, "stop"), (desktop, "close")):
            if target is None:
                continue
            try:
                await getattr(target, method)()
            except Exception as exc:
                message = str(exc).lower()
                if not any(marker in message for marker in ("transport closed", "transport is closed",
                    "writeunixtransport closed", "connection closed", "connection is closed",
                    "target page, context or browser has been closed")):
                    errors.append(exc)
        if desktop is not None and not getattr(desktop,'sessions',{}):self.desktop_runtime=None
        if errors:
            raise errors[0]
