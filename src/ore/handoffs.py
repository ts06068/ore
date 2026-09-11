"""Durable operator requests. Browser process IDs are replaceable references."""
from __future__ import annotations

import hashlib
import re
from fastapi import APIRouter, Request
from urllib.parse import urlsplit, urlunsplit

from .policy import redact
from .store import LeaseLost, DocumentConflict

ACTIVE = {'needs_user', 'claimed', 'waiting_external', 'recovering'}


def safe_checkpoint(url):
    if not isinstance(url, str):
        return None
    value = urlsplit(url)
    if value.scheme not in ('https', 'http') or not value.hostname or value.username or value.password:
        return None
    return urlunsplit((value.scheme, value.netloc, value.path or '/', '', ''))


def safe_browser_environment(value):
    """Persist only the bounded runtime classification, never raw browser data."""
    if not isinstance(value, dict) or value.get('code') not in {
        'debug_automation_detected', 'automation_signal_observed',
        'unverified_automation_message', 'runtime_support_unconfirmed'}:
        return None
    from .browser_diagnostics import classify_runtime_diagnostic, DEBUG_SOURCE
    observed = value.get('evidence') if isinstance(value.get('evidence'), dict) else {}
    try:
        origin = safe_checkpoint(observed.get('url_origin'))
    except ValueError:
        origin = None
    explicit = observed.get('explicit_automation_message') is True
    official = (value['code'] == 'debug_automation_detected' and explicit
                and observed.get('official_debug_page') is True and origin == DEBUG_SOURCE)
    # Recreate controlled explanatory text. An arbitrary context message cannot
    # promote a runtime signal to an official diagnostic or leak raw page data.
    result = classify_runtime_diagnostic(
        url=DEBUG_SOURCE if official else origin if origin != DEBUG_SOURCE else None,
        page_text='Automated Browser Detected' if explicit else None,
        navigator_webdriver=observed.get('navigator_webdriver') if type(observed.get('navigator_webdriver')) is bool else None)
    for key in ('url_sha256', 'page_text_sha256'):
        result['evidence'].pop(key, None)
        if isinstance(observed.get(key), str) and re.fullmatch(r'[a-f0-9]{64}', observed[key]):
            result['evidence'][key] = observed[key]
    return result


class HandoffService:
    def __init__(self, store):
        self.store = store

    def get(self, ident):
        value = self.store.get_document('handoff', ident)
        if value is None:
            raise KeyError(ident)
        return value

    def is_current(self, row):
        job = self.store.get_job(row['job_id'])
        return bool(job and row.get('request_revision', row['revision']) == job['revision']
                    and row.get('request_generation', row['generation']) == job['generation'])

    def list(self, job_id=None, status=None):
        rows = self.store.list_documents('handoff', job_id=job_id, all_generations=True)
        if status == 'active':
            rows = [row for row in rows if row['status'] in ACTIVE and self.is_current(row)]
        elif status:
            rows = [row for row in rows if row['status'] == status and (status not in ACTIVE or self.is_current(row))]
        return sorted(rows, key=lambda row: row['created_at'], reverse=True)

    def update(self, ident, changes, *, expected_version=None):
        old = self.get(ident)
        return self.store.put_document('handoff', ident, {**old, **changes,
                                       'request_revision': old.get('request_revision',old['revision']),
                                       'request_generation': old.get('request_generation',old['generation'])}, job_id=old['job_id'],
                                       expected_version=expected_version if expected_version is not None else old['state_version'])

    def create(self, job_id, kind, reason, session_id=None, task_id=None, source=None,
               operation=None, context=None):
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        context = context or {}
        environment = safe_browser_environment(context.get('browser_environment'))
        environment_fields = {'browser_environment': environment} if environment else {}
        for old in self.list(job_id, 'active'):
            if session_id and old.get('session_id') == session_id:
                try:return self.update(old['id'], {'reason': str(reason)[:800], **({'task_id': task_id} if task_id else {}), **environment_fields})
                except DocumentConflict:return self.get(old['id'])
            if not session_id and source and old.get('source') == source and old.get('operation') == operation:
                return old
        identity = [job_id, job['revision'], job['generation'], kind, session_id, task_id, source,
                    operation, context.get('episode'), context.get('challenge_id')]
        ident = hashlib.sha256(str(identity).encode()).hexdigest()[:32]
        previous = self.store.get_document('handoff', ident)
        if previous:
            if previous['status'] in ACTIVE:return previous
            ident=hashlib.sha256((ident+str(previous['updated_at'])).encode()).hexdigest()[:32]
        url = safe_checkpoint(context.get('url'))
        if not url:
            url = next((safe_checkpoint(x) for x in job['mission'].get('urls', []) if safe_checkpoint(x)), None)
        value = {'id': ident, 'request_revision': job['revision'], 'request_generation': job['generation'], 'kind': kind, 'status': 'needs_user', 'reason': str(reason)[:800],
                 'session_id': session_id, 'task_id': task_id, 'source': source, 'operation': operation,
                 'session_state': 'live' if session_id else 'unavailable', 'checkpoint_url': url,
                 'challenge_id': context.get('challenge_id'), 'control_epoch': context.get('epoch'),
                 'executor_id': context.get('executor_id'), 'access_profile_ref': job['mission'].get('access_profile_ref') or job['mission'].get('access_profile') or 'public',
                 'href': f'/handoffs/{ident}', 'job_href': f'/jobs/{job_id}', 'receipts': {}, **environment_fields}
        try:
            result = self.store.put_document('handoff', ident, value, job_id=job_id, expected_version=0)
        except (LeaseLost, DocumentConflict):
            return self.get(ident)
        self.store.append_event(job_id, 'handoff.created', {'handoff_id': ident, 'kind': kind, 'href': value['href']})
        return result

    def conversation_context(self, job_id):
        """Expose ownership facts without moving a browser or changing its authority."""
        job = self.store.get_job(job_id)
        if not job:
            return None
        run = self.store.get_document('workflow.run', job['workflow_run_id']) if job.get('workflow_run_id') else None
        planning = job.get('purpose') == 'planning_reconnaissance'
        conversation_id = job.get('conversation_id') if planning else (run or {}).get('plan', {}).get('conversation_id')
        if not planning and not run:
            # Older reconnaissance jobs predate purpose/conversation metadata.
            # The host's retained conversation record is authoritative linkage.
            linked = next((document.get('record', {}) for document in self.store.list_documents('conversation')
                           if document.get('record', {}).get('planning_job_id') == job_id), None)
            if linked:
                planning, conversation_id = True, linked['id']
        if not conversation_id:
            return {'phase': 'planning'} if planning else None
        document = self.store.get_document('conversation', conversation_id)
        record = (document or {}).get('record', {})
        active_run_id = record.get('active_run_id') or next(iter(reversed(record.get('run_ids', []))), None)
        active_run = self.store.get_document('workflow.run', active_run_id) if active_run_id else None
        active_job = (active_run or {}).get('job_id')
        return {'phase': 'planning' if planning else 'execution',
                'conversation_id': conversation_id, 'conversation_href': f'/chat/{conversation_id}',
                'planning_active': bool(record.get('planner_running')),
                'historical': bool(planning and active_run_id and not record.get('planner_running')),
                **({'active_execution_job_id': active_job,
                    'active_execution_status': active_run.get('status')} if active_job else {})}

    def public(self, row):
        if not self.is_current(row):
            row = {**row, 'superseded': True}
            if row['status'] in ACTIVE:
                row.update(status='cancelled', resolution='mission_contract_superseded',
                           reason='This request belongs to an earlier mission revision or refresh. Inspect the current mission for active requests.')
        context = self.conversation_context(row['job_id'])
        if context:
            row = {**row, 'conversation_context': context}
        if row.get('challenge_id'):
            from .operator_retry import retry_metadata
            row = {**row, 'automatic_retry': retry_metadata(self.store, row)}
        return redact({key: value for key, value in row.items() if key not in ('receipts', 'inflight')})

    def recover_interrupted_actions(self):
        for row in self.list(status='active'):
            if row.get('inflight'):
                self.update(row['id'],{'inflight':None,'status':'needs_user','reason':'The server restarted during an operator action. Inspect the current browser and verify access before resuming.'})

    def record_event(self, job_id, kind, payload):
        payload = payload or {}
        if kind == 'browser_handoff':
            return self.create(job_id, 'challenge' if payload.get('challenge_id') else 'browser',
                               payload.get('reason', 'Your input is required in the browser.'),
                               session_id=payload.get('session_id'), task_id=payload.get('task_id'), context=payload)
        if kind in ('browser_session_lost', 'executor.session_lost'):
            for row in self.list(job_id, 'active'):
                if row.get('session_id') == payload.get('session_id'):
                    self.update(row['id'], {'session_state': 'lost', 'status': 'needs_user',
                                           'reason': 'The browser session ended. Restore the session and verify access before resuming.'})
        if kind == 'tool_completed' and payload.get('tool') == 'handoff':
            result = payload.get('result') or {}
            return self.create(job_id, 'browser', result.get('reason') or 'Your input is required.',
                               session_id=result.get('session_id') or result.get('id'), context=result)
        if kind == 'browser_resumed':
            for row in self.list(job_id, 'active'):
                if row.get('session_id') == payload.get('session_id') and not row.get('inflight'):
                    self.update(row['id'], {'status': 'resolved', 'resolution': 'browser_resumed'})
        return None

    def begin_action(self, ident, action, expected_version, idempotency_key):
        if not re.fullmatch(r'[A-Za-z0-9._:-]{4,120}', idempotency_key or ''):
            raise ValueError('A 4–120 character idempotency_key is required')
        old = self.get(ident)
        receipt = old.get('receipts', {}).get(idempotency_key)
        if receipt:
            if receipt['action'] != action:
                raise LeaseLost('Idempotency key was already used for another action')
            return old, True
        if not self.is_current(old):
            raise LeaseLost('This handoff belongs to an earlier mission revision or refresh')
        if old.get('inflight'):
            raise LeaseLost('A handoff action is already in progress; reload the request')
        if old['status'] not in ACTIVE:
            raise LeaseLost('Handoff is no longer active')
        claimed = self.update(ident, {'inflight': {'key': idempotency_key, 'action': action}},
                              expected_version=expected_version)
        return claimed, False

    def finish_action(self, ident, action, key, changes=None, *, failed=False):
        old = self.get(ident)
        if old.get('inflight') != {'key': key, 'action': action}:
            raise LeaseLost('Handoff action ownership changed')
        receipts = dict(list(old.get('receipts', {}).items())[-49:])
        if not failed:
            receipts[key] = {'action': action, 'completed': True}
        result = self.update(ident, {**(changes or {}), 'inflight': None, 'receipts': receipts})
        self.store.append_event(old['job_id'], 'handoff.action_failed' if failed else 'handoff.action',
                                {'handoff_id': ident, 'action': action, 'status': result['status']})
        return result


class BrowserAccess:
    """Operator broker shared by HTTP, handoffs and executor-hosted browsers."""
    def __init__(self, engine):
        self.engine = engine

    @property
    def remote(self):
        manager = getattr(self.engine, 'execution', None)
        return manager if manager and manager.enabled else None

    def exists(self, sid):
        if not sid:
            return False
        if self.remote:
            return self.remote.has_session(sid) and not self.remote.get_session(sid).closed
        try:
            self.engine.browser.get(sid)
            return True
        except Exception:
            return False

    def session(self, sid):
        return self.remote.get_session(sid) if self.remote else self.engine.browser.get(sid)

    async def summary(self, sid):
        return await self.remote.session_summary(sid) if self.remote else await self.engine.browser.summary(self.session(sid))

    async def sessions(self):
        return await self.remote.list_sessions() if self.remote else await self.engine.browser.list()

    async def create(self, job, profile, *, task_id=None):
        agent_id = None
        if task_id:
            task = self.engine.store.get_task(task_id)
            if not task or task['job_id'] != job['id'] or task['revision'] != job['revision'] or task['generation'] != job['generation']:
                raise LeaseLost('The browser task no longer belongs to this mission revision')
            agent_id = 'task:' + task_id
        if self.remote:
            value = await self.remote.create_session(job['id'], job['mission'], profile,
                                                     agent_id=agent_id, task_id=task_id)
            return value['id'] if isinstance(value, dict) else value.id
        return (await self.engine.browser.create(job['id'], job['mission'], profile, agent_id=agent_id)).id

    async def command(self, sid, action, args=None):
        args = args or {}
        if self.remote:
            return await self.remote.browser_command(sid, action, args)
        browser, session = self.engine.browser, self.session(sid)
        if action == 'observe':
            return await browser.observe(sid, owner='human', screenshot=False)
        if action == 'action':
            return await browser.action(sid, args['action'], args, owner='human')
        if action == 'takeover':
            return await browser.takeover(sid)
        if action == 'resume':
            return await browser.resume(sid)
        if action == 'verify_challenge':
            async with session.lock:
                await browser._authorize(session, 'human', args.get('epoch'))
                if args.get('checkpoint_url'):
                    session.challenge_url = args['checkpoint_url']
                    value = urlsplit(args['checkpoint_url'])
                    session.challenge_origin = f'{value.scheme}://{value.netloc}'
                recovered, evidence = await browser._target_recovered(session)
                challenge_id = args.get('challenge_id') or session.challenge_id
                if recovered and challenge_id:
                    self.engine.store.resolve_challenge(challenge_id, evidence)
                return {'resolved': recovered, 'evidence': evidence}
        if action == 'save_profile':
            await browser.save_profile(session)
            return {'saved': True}
        if action in ('secret-fill', 'secret-capture'):
            from .policy import AccessDenied
            if session.control != 'human':
                raise AccessDenied('Secret operations require human browser control')
            async with session.lock:
                await browser._authorize(session, 'human', args.get('epoch', session.epoch))
                target = session.page.locator(args['selector'])
                if action == 'secret-fill':
                    value = self.engine.secrets.get(args['ref'])
                    if value is None:
                        raise AccessDenied('Secret reference is unavailable')
                    await target.fill(value)
                else:
                    value = await target.input_value() if await target.evaluate("e => 'value' in e") else await target.inner_text()
                    self.engine.secrets.set(args['ref'], value.strip())
            return {'completed': True, 'ref': args['ref']}
        raise ValueError('Unsupported operator browser operation')


def create_handoff_router(engine, browser):
    from fastapi import APIRouter, Request
    from datetime import datetime, timezone
    from .policy import AccessDenied
    import contextlib

    router = APIRouter(prefix='/v1/handoffs', tags=['handoffs'])

    async def output(row):
        if not engine.handoffs.is_current(row):return engine.handoffs.public(row)
        sid = row.get('session_id')
        changes = {}
        if sid and browser.exists(sid):
            summary = await browser.summary(sid)
            changes = {'session_state': 'live', 'control_epoch': summary.get('epoch'),
                       'checkpoint_url': row.get('checkpoint_url') or safe_checkpoint(summary.get('url')),
                       'executor_id': summary.get('worker_id') or summary.get('executor_id')}
            if summary.get('transport'):
                changes['browser_transport'] = summary['transport']
            if 'browser_environment' in summary:
                changes['browser_environment'] = safe_browser_environment(summary.get('browser_environment'))
        elif sid:
            changes = {'session_state': 'lost'}
        changes = {key: value for key, value in changes.items() if row.get(key) != value}
        if changes:
            with contextlib.suppress(LeaseLost, DocumentConflict):
                row = engine.handoffs.update(row['id'], changes)
        return engine.handoffs.public(row)

    router.handoff_output = output

    @router.get('')
    async def list_requests(job_id: str | None = None, status: str | None = None):
        return [await output(row) for row in engine.handoffs.list(job_id, status)]

    @router.get('/{ident}')
    async def get_request(ident: str):
        return await output(engine.handoffs.get(ident))

    @router.post('/{ident}/actions')
    async def act(ident: str, request: Request):
        body = await request.json()
        action, key, version = body.get('action'), body.get('idempotency_key'), body.get('expected_version')
        if action not in ('claim', 'recreate_session', 'resume', 'retry_verification', 'exclude_operation', 'mark_pending', 'cancel'):
            raise ValueError('Unsupported handoff action')
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError('expected_version is required')
        if action == 'retry_verification':
            from .desktop_api import _operator
            _operator(request, engine)
        current = engine.handoffs.get(ident)
        sid = current.get('session_id')
        receipt = current.get('receipts', {}).get(key)
        if receipt:
            if receipt['action'] != action:raise LeaseLost('Idempotency key was already used')
            if action == 'retry_verification':
                from .operator_retry import resume_authorized_retry
                current = await resume_authorized_retry(engine, current, key)
            return await output(current)
        if action in ('claim', 'resume') and sid and browser.exists(sid):
            if body.get('expected_epoch') != (await browser.summary(sid)).get('epoch'):
                raise LeaseLost('Browser control changed; reload the request')
        row, replay = engine.handoffs.begin_action(ident, action, version, key)
        if replay:
            return await output(row)
        record = engine.store.get_job(row['job_id'])
        changes = {}
        try:
            if action == 'claim':
                if not browser.exists(sid):
                    raise AccessDenied('Browser session was lost. Recreate it first.')
                summary = await browser.command(sid, 'takeover', {'epoch': body['expected_epoch']})
                changes = {'status': 'claimed', 'control_epoch': summary['epoch']}
            elif action == 'recreate_session':
                if browser.exists(sid):
                    raise LeaseLost('A live session already exists')
                target = safe_checkpoint(body.get('url')) or row.get('checkpoint_url')
                if not target:
                    raise ValueError('A safe starting URL is required')
                sid = await browser.create(record, engine.profile(record['mission']), task_id=row.get('task_id'))
                engine.handoffs.update(ident, {'session_id': sid, 'session_state': 'live'})
                summary = await browser.command(sid, 'takeover')
                await browser.command(sid, 'action', {'action': 'navigate', 'url': target, 'epoch': summary['epoch']})
                changes = {'status': 'claimed', 'session_id': sid, 'session_state': 'live', 'control_epoch': summary['epoch'], 'checkpoint_url': target}
            elif action == 'retry_verification':
                from .operator_retry import retry_verification
                changes = await retry_verification(engine, browser, row, body)
            elif action == 'resume':
                if row.get('source'):
                    from .source_policy import source_readiness
                    readiness = source_readiness(row['source'], row['operation'], engine.profile(record['mission']))
                    if readiness['state'] != 'ready':
                        raise AccessDenied('Verify this source operation or exclude it before resuming')
                if sid:
                    if not browser.exists(sid):
                        raise AccessDenied('Browser session was lost. Recreate it first.')
                    observation = await browser.command(sid, 'observe')
                    if observation.get('challenge_detected'):
                        raise AccessDenied('The browser still displays a challenge')
                    if row.get('challenge_id'):
                        result = await browser.command(sid, 'verify_challenge', {'epoch': body['expected_epoch'], 'challenge_id': row['challenge_id'], 'checkpoint_url': row.get('checkpoint_url')})
                        if not result.get('resolved'):
                            raise AccessDenied('The original target has not recovered')
                    await browser.command(sid, 'resume', {'epoch': body['expected_epoch']})
                planning = engine.handoffs.conversation_context(record['id'])
                if planning and planning.get('phase') == 'planning':
                    changes = {'status': 'resolved', 'resolution': 'planning_access_verified',
                        'execution_resumed': False,
                        'reason': 'Planning browser access was verified. Return to the conversation to continue planning or review the current execution; this browser does not resume the collection.'}
                else:
                    changes = {'status': 'resolved', 'resolution': 'verified_and_resumed'}
            elif action == 'exclude_operation':
                from .source_policy import normalize_source_policy, canonical_operation
                source = row.get('source') or body.get('source')
                if not source:
                    raise ValueError('A source is required')
                operation = canonical_operation(row.get('operation') or body.get('operation', 'search'))
                mission = dict(record['mission']); policy = normalize_source_policy(mission)
                policy['exclude'][operation] = sorted(set(policy['exclude'].get(operation, [])) | {source})
                mission['source_policy'] = policy
                if operation == 'search':
                    mission['sources'] = [x for x in mission.get('sources', []) if x != source]
                await engine.revise(record['id'], mission)
                changes = {'status': 'resolved', 'resolution': 'operation_excluded', 'source': source, 'operation': operation}
            elif action == 'mark_pending':
                if not row.get('source') or not row.get('operation'):
                    raise ValueError('Only a source setup can wait for provider approval')
                profile = engine.profile(record['mission'])
                profile.setdefault('source_readiness', {}).setdefault(row['source'], {})[row['operation']] = {
                    'state': 'approval_pending', 'observed_at': datetime.now(timezone.utc).isoformat(),
                    'evidence_ref': f'handoff:{ident}', 'scope': 'operator_reported_provider_approval'}
                engine.save_profile(profile)
                if sid and browser.exists(sid):
                    await browser.command(sid, 'save_profile')
                changes = {'status': 'waiting_external', 'reason': 'Provider approval is pending. Exclude this API operation to continue with other routes.'}
            elif action == 'cancel':
                changes = {'status': 'cancelled', 'resolution': 'operator_cancelled'}
            result = engine.handoffs.finish_action(ident, action, key, changes)
            if action == 'retry_verification':
                from .operator_retry import resume_authorized_retry
                result = await resume_authorized_retry(engine, result, key)
            elif action == 'resume' and result.get('resolution') != 'planning_access_verified':
                await engine.run(record['id'])
            return await output(result)
        except BaseException:
            with contextlib.suppress(Exception):
                engine.handoffs.finish_action(ident, action, key, failed=True)
            raise
    return router
