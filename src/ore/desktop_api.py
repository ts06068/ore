"""Authenticated operator attachment of an ORE-owned native desktop to a handoff."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
import secrets
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .companion import manual_policy
from .desktop_browser import desktop_origins, issue_checkpoint_identity
from .handoffs import ACTIVE, safe_checkpoint, safe_browser_environment
from .policy import AccessDenied, AccessPolicy
from .store import LeaseLost


class DesktopAttachRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    handoff_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9._:-]{4,120}$')


def _operator(request, engine):
    bearer = request.headers.get('authorization', '')
    token = bearer[7:] if bearer.startswith('Bearer ') else request.cookies.get('ore_session', '')
    expected = engine.settings.auth_token
    if not token or not expected or not secrets.compare_digest(str(token), str(expected)):
        raise HTTPException(401, 'Operator authentication required')
    origin = request.headers.get('origin')
    if origin and (urlsplit(origin).netloc != request.url.netloc or urlsplit(origin).scheme != request.url.scheme):
        raise HTTPException(403, 'Cross-origin mutations are disabled')


def _session_policy(mission, profile, checkpoint=None):
    from .native_scope import native_scope_copy
    policy = manual_policy(SimpleNamespace(mission=mission, policy=SimpleNamespace(profile=profile)))
    original_origins = policy.mission.get('allowed_origins') or (policy.mission.get('scope') or {}).get('origins') or []
    # Explicit operator attachment may select native transport over companion.
    copied_profile = deepcopy(policy.profile)
    copied_profile.pop('require_companion', None)
    copied_profile['browser_backend'] = 'desktop_chrome'
    copied_mission, copied_profile = native_scope_copy(policy.mission, copied_profile, checkpoint)
    additions = sorted(set(copied_mission['allowed_origins']) - set(original_origins))
    return copied_mission, copied_profile, additions


def _native(session):
    return bool(session and not session.closed and getattr(session.context, 'desktop', False))


def _current(engine, handoff, job, profile):
    latest = engine.store.get_job(job['id'])
    if not latest or (latest['revision'], latest['generation']) != (job['revision'], job['generation']):
        raise LeaseLost('Mission changed while attaching the desktop')
    if not engine.handoffs.is_current(handoff):
        raise LeaseLost('This handoff belongs to an earlier mission revision or refresh')
    if deepcopy(engine.profile(latest['mission'])) != profile:
        raise LeaseLost('Access profile changed while attaching the desktop; reload the request')
    task_id = handoff.get('task_id')
    if task_id:
        task = engine.store.get_task(task_id)
        if not task or (task['job_id'], task['revision'], task['generation']) != (job['id'], job['revision'], job['generation']):
            raise LeaseLost('The handoff task no longer belongs to this mission revision')


def create_desktop_router(engine):
    router = APIRouter(prefix='/v1/desktop')

    def duplicates(handoff, session_id, *, failed=False):
        # take_over emits a browser_handoff event; retain the requested stable link.
        for row in engine.handoffs.list(handoff['job_id'], 'active'):
            if row['id'] != handoff['id'] and row.get('session_id') == session_id:
                engine.handoffs.update(row['id'], {'status': 'cancelled',
                    'resolution': 'desktop_attachment_failed' if failed else 'attached_to_existing_handoff',
                    'related_handoff_id': handoff['id']})

    @router.get('/preview')
    async def preview(handoff_id: str, request: Request):
        _operator(request, engine)
        if getattr(getattr(engine, 'execution', None), 'enabled', False):
            raise AccessDenied('ORE desktop attachment currently requires the local coordinator execution backend')
        h = engine.handoffs.get(handoff_id)
        if not engine.handoffs.is_current(h) or h['status'] not in ACTIVE:
            raise LeaseLost('This handoff is no longer current and active')
        job = engine.store.get_job(h['job_id'])
        from .operator_access import attachment_mission
        mission, basis = attachment_mission(job, h)
        original_profile = engine.profile(job['mission'])
        ref = job['mission'].get('access_profile_ref') or job['mission'].get('access_profile') or 'public'
        if h.get('access_profile_ref', ref) != ref or original_profile.get('id', 'public') != ref:
            raise AccessDenied('Handoff access profile does not match the current mission')
        mission, profile, additions = _session_policy(mission, original_profile, safe_checkpoint(h.get('checkpoint_url')))
        checkpoint = safe_checkpoint(h.get('checkpoint_url'))
        if not checkpoint:
            raise AccessDenied('A persisted HTTP(S) checkpoint is required')
        await desktop_origins(mission, profile)
        await AccessPolicy(mission, profile).check(checkpoint)
        _current(engine, h, job, original_profile)
        return {'handoff_id': h['id'], 'expected_version': h['state_version'],
                'eligible': True, 'basis': basis, 'scope': 'operator_session_only',
                'origins': mission['allowed_origins'], 'checkpoint_url': checkpoint,
                'challenge_budget_preserved': True}

    @router.post('/attach')
    async def attach(body: DesktopAttachRequest, request: Request):
        _operator(request, engine)
        if getattr(getattr(engine, 'execution', None), 'enabled', False):
            raise AccessDenied('ORE desktop attachment currently requires the local coordinator execution backend')
        h = engine.handoffs.get(body.handoff_id)
        if not engine.handoffs.is_current(h):
            raise LeaseLost('This handoff belongs to an earlier mission revision or refresh')
        if h['status'] not in ACTIVE:
            raise LeaseLost('Handoff is no longer active')
        job = engine.store.get_job(h['job_id'])
        profile = deepcopy(engine.profile(job['mission']))
        ref = job['mission'].get('access_profile_ref') or job['mission'].get('access_profile') or 'public'
        if h.get('access_profile_ref', ref) != ref or profile.get('id', 'public') != ref:
            raise AccessDenied('Handoff access profile does not match the current mission')
        checkpoint = safe_checkpoint(h.get('checkpoint_url'))
        if not checkpoint:
            raise AccessDenied('This handoff needs a valid HTTP(S) checkpoint before desktop attachment')
        _current(engine, h, job, profile)
        old = engine.browser.sessions.get(h.get('session_id'))
        if old and (old.job_id != job['id'] or old.profile_id != ref or
                    old.principal_id != str(profile.get('principal_id', 'operator'))):
            raise AccessDenied('Existing browser identity does not match this handoff')
        h, replay = engine.handoffs.begin_action(h['id'], 'attach_desktop', body.expected_version, body.idempotency_key)
        if replay:
            return engine.handoffs.public(h)
        created = None
        committed = False
        try:
            from .operator_access import attachment_mission
            attachment_scope, scope_basis = attachment_mission(job, h)
            mission_copy, profile_copy, additions = _session_policy(attachment_scope, profile, checkpoint)
            if issue_checkpoint_identity(checkpoint):
                mission_copy['desktop_issue_checkpoint'] = checkpoint
            # This includes access-profile bounds, DNS/private-address checks, and
            # source operations. Support origins become explicit navigation scope.
            admitted = await desktop_origins(mission_copy, profile_copy)
            await AccessPolicy(mission_copy, profile_copy).check(checkpoint)
            _current(engine, h, job, profile)
            if _native(old):
                if old.mission != mission_copy or old.policy.profile != profile_copy:
                    raise AccessDenied('Existing desktop policy differs from the current attachment scope; close it before recreating')
                session = old
                additions = (h.get('desktop_attachment') or {}).get('added_native_origins', additions)
            else:
                session = created = await engine.browser.create(job['id'], mission_copy, profile_copy,
                    agent_id='task:' + h['task_id'] if h.get('task_id') else None)
                if not _native(session):
                    raise AccessDenied('The requested native desktop transport was not created')
                if session.profile_id != ref or session.principal_id != str(profile.get('principal_id', 'operator')):
                    raise AccessDenied('New desktop identity does not match this handoff')
            summary = await engine.browser.takeover(session.id, user_id='operator')
            if session.control != 'human' or session.human_id != 'operator':
                raise AccessDenied('The desktop must remain under operator control')
            if safe_checkpoint(session.page.url) != checkpoint:
                await engine.browser.action(session.id, 'navigate', {'url': checkpoint, 'epoch': summary['epoch']}, owner='human')
            observation = await engine.browser.observe(session.id, owner='human', screenshot=True)
            latest = getattr(session.page, 'latest', {})
            if (latest.get('url_observed') is not True or safe_checkpoint(latest.get('url')) != checkpoint
                    or safe_checkpoint(session.page.url) != checkpoint
                    or not re.fullmatch(r'[a-f0-9]{64}', str(latest.get('screenshot_sha256', '')))
                    or observation.get('page_state') == 'error'):
                raise AccessDenied('The checkpoint was not observed in the native desktop; existing session preserved')
            _current(engine, h, job, profile)
            current = engine.handoffs.get(h['id'])
            if current.get('inflight') != {'key': body.idempotency_key, 'action': 'attach_desktop'}:
                raise LeaseLost('Handoff action ownership changed while attaching the desktop')
            evidence = {'added_native_origins': additions, 'admitted_native_origins': admitted,
                'omitted_native_origins': mission_copy.get('desktop_omitted_origins', []),
                'support_only_enforcement': False, 'scope': 'operator_session_only', 'scope_basis': scope_basis,
                'screenshot_sha256': latest['screenshot_sha256'],
                'checkpoint_sha256': hashlib.sha256(checkpoint.encode()).hexdigest(),
                'reused_session': created is None, 'resource_mode': getattr(engine.settings, 'desktop_resource_mode', 'cgroup')}
            changes = {'session_id': session.id, 'session_state': 'live', 'control_epoch': session.epoch,
                'status': 'claimed', 'browser_transport': 'desktop_chrome', 'desktop_attachment': evidence,
                'reason': 'ORE Chrome desktop is attached. Verify access in this browser before resuming.'}
            environment = safe_browser_environment(observation.get('browser_environment'))
            # A Playwright-specific warning must not survive a native transport swap.
            changes['browser_environment'] = environment
            result = engine.handoffs.finish_action(h['id'], 'attach_desktop', body.idempotency_key, changes)
            committed = True
            engine.store.append_event(job['id'], 'desktop.attached', {'handoff_id': h['id'], 'session_id': session.id, **evidence})
            duplicates(h, session.id)
            # Only the verified, committed replacement may close the old browser.
            if created and old and not old.closed:
                try:
                    await engine.browser.close_session(old.id)
                except Exception as exc:
                    engine.store.append_event(job['id'], 'desktop.old_session_cleanup_failed',
                        {'handoff_id': h['id'], 'session_id': old.id, 'error_code': type(exc).__name__})
            return engine.handoffs.public(result)
        except BaseException:
            if not committed:
                if created and not created.closed:
                    try:
                        await engine.browser.close_session(created.id)
                    except Exception:
                        pass
                    duplicates(h, created.id, failed=True)
                current = engine.handoffs.get(h['id'])
                if current.get('inflight') == {'key': body.idempotency_key, 'action': 'attach_desktop'}:
                    engine.handoffs.finish_action(h['id'], 'attach_desktop', body.idempotency_key,
                        {'reason': 'Desktop attachment did not complete. The previous session and challenge budget were preserved.'}, failed=True)
            raise

    @router.post('/close')
    async def close(body: DesktopAttachRequest, request: Request):
        _operator(request, engine)
        if getattr(getattr(engine, 'execution', None), 'enabled', False):
            raise AccessDenied('ORE desktop control currently requires the local coordinator execution backend')
        h = engine.handoffs.get(body.handoff_id)
        if not engine.handoffs.is_current(h) or h['status'] not in ACTIVE:
            raise LeaseLost('This handoff is no longer current and active')
        job = engine.store.get_job(h['job_id'])
        profile = deepcopy(engine.profile(job['mission']))
        _current(engine, h, job, profile)
        session = engine.browser.sessions.get(h.get('session_id'))
        ref = job['mission'].get('access_profile_ref') or job['mission'].get('access_profile') or 'public'
        if (h.get('browser_transport') != 'desktop_chrome' or session is None
                or not getattr(session.context, 'desktop', False) or session.job_id != job['id']
                or session.profile_id != ref or session.principal_id != str(profile.get('principal_id', 'operator'))
                or h.get('access_profile_ref', ref) != ref):
            raise AccessDenied('This handoff does not own the requested native desktop')
        h, replay = engine.handoffs.begin_action(h['id'], 'close_desktop', body.expected_version, body.idempotency_key)
        if replay:
            return engine.handoffs.public(h)
        try:
            if not session.closed:
                if session.control != 'human' or session.human_id != 'operator':
                    raise AccessDenied('Only the current operator can close this desktop')
                if h.get('control_epoch') is not None and h['control_epoch'] != session.epoch:
                    raise LeaseLost('Desktop control changed; reload the handoff before closing')
                _current(engine, h, job, profile)
                await engine.browser.close_session(session.id)
            result = engine.handoffs.finish_action(h['id'], 'close_desktop', body.idempotency_key,
                {'session_state': 'lost', 'status': 'needs_user',
                 'reason': 'The ORE desktop was closed by the operator. Attach a desktop to continue this request.'})
            engine.store.append_event(job['id'], 'desktop.closed',
                {'handoff_id': h['id'], 'session_id': session.id, 'reason': 'operator_requested'})
            return engine.handoffs.public(result)
        except BaseException:
            current = engine.handoffs.get(h['id'])
            if current.get('inflight') == {'key': body.idempotency_key, 'action': 'close_desktop'}:
                engine.handoffs.finish_action(h['id'], 'close_desktop', body.idempotency_key,
                    {'reason': 'Desktop close did not complete. Reload the handoff and inspect its current session.'}, failed=True)
            raise

    return router
