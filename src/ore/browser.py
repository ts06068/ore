"""Server-side browser sessions with fenced control and encrypted session state."""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .config import SecretStore
from .models import canonical_digest
from .policy import AccessDenied, AccessPolicy, RateLimiter, redact
from .store import ControlConflict


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
    observers: list = field(default_factory=list)
    cdp: object = None
    closed: bool = False
    elements: list = field(default_factory=list)
    last_status: int | None = None
    challenge_id: str | None = None
    challenge_origin: str | None = None
    challenge_url: str | None = None
    challenge_reservation: dict | None = None


class BrowserManager:
    def __init__(self, settings, limiter: RateLimiter, on_download=None, on_event=None, store=None, secrets=None):
        self.settings, self.limiter = settings, limiter
        self.on_download, self.on_event, self.store = on_download, on_event, store
        self.secrets = secrets or SecretStore(settings.state_dir)
        self.sessions: dict[str, BrowserSession] = {}
        self.playwright = self.browser = None
        self.start_lock, self.profile_lock, self.create_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()

    async def _emit(self, job_id, kind, payload):
        if self.on_event:
            result = self.on_event(job_id, kind, redact(payload))
            if inspect.isawaitable(result):
                await result

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
        await self.start()
        profile = profile or {}
        sid, profile_id = uuid.uuid4().hex, str(profile.get("id", "public"))
        principal = str(profile.get("principal_id", "operator"))
        ref = "browser-profile:" + canonical_digest([principal, profile_id])
        persist = bool(profile.get("persist_session", profile_id != "public"))
        cap = int(profile.get("max_browser_sessions", max(4, self.settings.max_workers) if profile_id == "public" else 1))
        active = [item for item in self.sessions.values() if not item.closed and item.profile_ref == ref]
        if len(active) >= cap:
            raise AccessDenied("Access profile browser session limit reached")
        options = {"accept_downloads": True, "viewport": {"width": 1280, "height": 800}, "service_workers": "block"}
        if persist:
            async with self.profile_lock:
                saved = await asyncio.to_thread(self.secrets.get, ref)
            if saved:
                options["storage_state"] = json.loads(saved)
        if profile.get("proxy"):
            options["proxy"] = {"server": profile["proxy"]}
        context = await self.browser.new_context(**options)
        page = await context.new_page()
        session = BrowserSession(sid, job_id, context, page, AccessPolicy(mission, profile), profile_id,
            float(mission.get("limits", {}).get("origin_min_interval_seconds", 3)),
            agent_id=agent_id or f"agent:{sid}", principal_id=principal, profile_ref=ref,
            persist_profile=persist, mission=mission)
        self.sessions[sid] = session
        if self.store:
            control = await asyncio.to_thread(self.store.acquire_control, job_id, sid, session.agent_id, "agent", None, 3600)
            session.epoch = control["epoch"]
            await asyncio.to_thread(self.store.record_observation, job_id,
                {"kind": "browser.created", "session_id": sid, "profile_id": profile_id, "agent_id": session.agent_id})

        async def route(request_route):
            request = request_route.request
            try:
                await session.policy.check(request.url)
                await self.limiter.acquire(f"{session.profile_id}:{urlsplit(request.url).hostname}", session.interval)
                await request_route.continue_()
            except AccessDenied as exc:
                await self._emit(job_id, "request_blocked", {"url": request.url, "reason": str(exc)})
                await request_route.abort("blockedbyclient")

        await context.route("**/*", route)

        async def response_seen(response):
            if response.request.is_navigation_request() and response.frame == session.page.main_frame:
                session.last_status = response.status
            if response.status == 429:
                try:
                    delay = float((await response.all_headers()).get("retry-after", "30"))
                except ValueError:
                    delay = 30
                await self.limiter.penalize(f"{profile_id}:{urlsplit(response.url).hostname}", min(max(delay, 0), 3600))
            if response.status in (401, 403, 429):
                await self._emit(job_id, "browser.http_status", {"url": response.url, "status": response.status})

        async def download(item):
            path = self.settings.state_dir / "staging" / uuid.uuid4().hex
            path.parent.mkdir(parents=True, exist_ok=True)
            value = {"filename": item.suggested_filename, "url": item.url, "session_id": sid,
                     "observed_page_url": session.page.url, "status": "pending"}
            try:
                await item.save_as(path)
                failure = await item.failure()
                if failure:
                    raise AccessDenied("Browser download failed: " + failure)
                maximum = int(mission.get("limits", {}).get("max_artifact_bytes", 256 * 1024 * 1024))
                if path.stat().st_size > maximum:
                    raise AccessDenied("Browser download exceeds artifact byte limit")
                value.update(path=str(path), bytes=path.stat().st_size, status="staged")
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
            new_page.on("response", lambda response: asyncio.create_task(response_seen(response)))

        attach(page)
        context.on("page", attach)
        await self._connect_frames(session)
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
        return {"id": session.id, "session_id": session.id, "job_id": session.job_id,
                "url": session.page.url, "title": title, "control": session.control,
                "epoch": session.epoch, "width": 1280, "height": 800,
                "challenge_id": session.challenge_id}

    async def list(self):
        return [await self.summary(session) for session in self.sessions.values() if not session.closed]

    async def _authorize(self, session, owner, epoch=None, owner_id=None):
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
        await self._emit(session.job_id, "browser_handoff", {"session_id": session.id, "epoch": session.epoch})
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
        if not session.persist_profile:
            return
        state = await session.context.storage_state()
        async with self.profile_lock:
            await asyncio.to_thread(self.secrets.set, session.profile_ref, json.dumps(state))

    async def _challenge_visible(self, session):
        if session.page.url == "about:blank":
            return False
        selectors = '[data-ore-challenge="true"]:visible,#challenge-form:visible,#cf-challenge-running:visible,iframe[src*="challenges.cloudflare.com"]:visible'
        if await session.page.locator(selectors).count():
            return True
        title = (await session.page.title()).strip().lower()
        return title in ("just a moment...", "verify you are human", "checking your browser", "attention required! | cloudflare")

    async def _target_recovered(self, session):
        if await self._challenge_visible(session):
            return False, {"reason": "challenge_still_visible"}
        origin = f"{urlsplit(session.page.url).scheme}://{urlsplit(session.page.url).netloc}"
        if session.challenge_origin and origin != session.challenge_origin:
            return False, {"reason": "different_origin_is_not_resolution"}
        if session.challenge_url:
            expected_url, current_url = urlsplit(session.challenge_url), urlsplit(session.page.url)
            if expected_url.path != current_url.path and not session.policy.profile.get("challenge_success_selector"):
                return False, {"reason": "different_page_requires_a_configured_success_check"}
        if await session.page.locator('input[type="password"]').count():
            return False, {"reason": "authentication_required"}
        if session.last_status is not None and session.last_status >= 400:
            return False, {"reason": "http_error", "status": session.last_status}
        selector = session.policy.profile.get("challenge_success_selector")
        text = (await session.page.locator("body").inner_text()) if session.page.url != "about:blank" else ""
        present = bool(await session.page.locator(selector).count()) if selector else len(text.strip()) >= 80
        return present, {"url": redact(session.page.url), "status": session.last_status,
                         "success_selector": selector, "substantive_content_observed": present}

    async def challenge(self, sid):
        session = self.get(sid)
        async with session.lock:
            await self._authorize(session, "agent")
            if not await self._challenge_visible(session):
                return {"allowed": False, "reason": "no_challenge_observed", **await self.summary(session)}
            if not self.store:
                raise AccessDenied("Durable challenge accounting requires a Store")
            origin = f"{urlsplit(session.page.url).scheme}://{urlsplit(session.page.url).netloc}"
            config = session.mission.get("on_challenge", {})
            reservation = await asyncio.to_thread(self.store.reserve_challenge, session.job_id, origin,
                session.profile_id + ":" + session.principal_id,
                int(config.get("max_attempts_per_episode", 3)), float(config.get("max_active_seconds", 120)))
            session.challenge_id, session.challenge_origin, session.challenge_url = reservation["id"], origin, session.page.url
            if reservation["allowed"]:
                session.challenge_reservation = reservation
            else:
                await self._takeover_locked(session)
                await asyncio.to_thread(self.store.update_job, session.job_id, status="awaiting_user")
            return {key: value for key, value in reservation.items() if key != "token"}

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
        text = (await page.locator("body").inner_text(timeout=15000))[:30000] if page.url != "about:blank" else ""
        elements = await page.locator('a,button,input,textarea,select,[role="button"]').evaluate_all('''els => els.map((e,i)=>({
            index:i,tag:e.tagName.toLowerCase(),text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder')||'').slice(0,200),
            type:e.getAttribute('type'),name:e.getAttribute('name'),href:e.href||null,
            visible:!!(e.getClientRects().length)
        })).filter(e=>e.visible).slice(0,180)''') if page.url != "about:blank" else []
        session.elements = elements
        result = {**await self.summary(session), "text": text, "elements": elements,
            "downloads": redact(list(session.downloads)), "challenge_detected": await self._challenge_visible(session),
            "tabs": [{"index": i, "url": redact(item.url)} for i, item in enumerate(session.context.pages)]}
        if self.store:
            await asyncio.to_thread(self.store.record_observation, session.job_id,
                {"kind": "browser.page", "session_id": sid, "source_url": redact(page.url),
                 "url": redact(page.url), "title": result["title"], "text_excerpt": text[:4000],
                 "elements": redact(elements), "challenge_detected": result["challenge_detected"]})
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
            visible_challenge = owner == "agent" and await self._challenge_visible(session)
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
            if isinstance(target, int):
                if not any(item["index"] == target for item in session.elements):
                    raise AccessDenied("Target index was not present in the latest observation")
                locator = page.locator('a,button,input,textarea,select,[role="button"]').nth(target)
            else:
                locator = page.locator(args["selector"]) if args.get("selector") else None
            started, reservation = time.monotonic(), session.challenge_reservation
            timeout = 90.0
            if reservation:
                timeout = max(0.0, min(timeout, (datetime.fromisoformat(reservation["expires_at"]) - datetime.now(timezone.utc)).total_seconds()))
            try:
                async with asyncio.timeout(timeout):
                    if action == "navigate":
                        await session.policy.check(args["url"])
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
                    recovered, evidence = await self._target_recovered(session)
                    result = await asyncio.to_thread(self.store.finish_challenge, reservation["id"],
                        reservation["token"], time.monotonic() - started, recovered, evidence if recovered else None)
                    session.challenge_reservation = None
                    if result["state"] == "awaiting_user":
                        await self._takeover_locked(session)
                        await asyncio.to_thread(self.store.update_job, session.job_id, status="awaiting_user")
                await self._emit(session.job_id, "browser.action", {"session_id": sid, "action": action,
                    "owner": owner, "epoch": session.epoch, "elapsed_seconds": time.monotonic() - started})
            if owner == "human" or session.control != "agent":
                return await self.summary(session)
            return await self._observe_unlocked(sid, screenshot=args.get("screenshot", True), owner_id=owner_id)

    async def close_session(self, sid):
        session = self.get(sid)
        async with session.lock:
            if session.download_tasks:
                await asyncio.gather(*list(session.download_tasks), return_exceptions=True)
            await self.save_profile(session)
            if self.store:
                identity = session.agent_id if session.control == "agent" else session.human_id
                try:
                    await asyncio.to_thread(self.store.release_control, session.job_id, sid, identity, session.epoch)
                except ControlConflict:
                    pass
            session.closed = True
            await session.context.close()

    async def close(self):
        for session in list(self.sessions.values()):
            if not session.closed:
                try:
                    await self.close_session(session.id)
                except Exception:
                    session.closed = True
        browser, playwright = self.browser, self.playwright
        self.browser = self.playwright = None
        errors = []
        for target, method in ((browser, "close"), (playwright, "stop")):
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
        if errors:
            raise errors[0]
