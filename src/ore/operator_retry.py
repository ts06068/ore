"""Explicit operator reauthorization of one expired browser verification window.

This module is not an agent capability. It preserves the previous episode and
cannot be invoked by opening a fresh browser or retrying an ordinary tool call.
"""
from copy import deepcopy
from datetime import timedelta
from urllib.parse import urlsplit

from . import challenge_policy
from .handoffs import safe_checkpoint
from .models import canonical_digest
from .policy import AccessDenied
from .store import ControlConflict

ATTEMPTS = 1
SECONDS = 120



def _latest_conversation_run(store, job):
    run = store.get_document('workflow.run', job.get('workflow_run_id')) if job.get('workflow_run_id') else None
    ident = (run or {}).get('plan', {}).get('conversation_id')
    if not ident:
        return True
    record = (store.get_document('conversation', ident) or {}).get('record', {})
    latest = record.get('active_run_id') or next(iter(reversed(record.get('run_ids', []))), None)
    return latest == run['id']

def retry_metadata(store, row):
    value = store.get_challenge(row['challenge_id']) if row.get('challenge_id') else None
    job = store.get_job(row['job_id'])
    policy = challenge_policy.normalize_policy((job or {}).get('mission', {}).get('on_challenge'))
    eligible, reason = False, 'This request has no expired verification window.'
    seconds = min(SECONDS, float((value or {}).get('hard_max_elapsed_seconds', SECONDS)))
    if value and value.get('clock') == 'elapsed':
        if value.get('operator_retry'):
            reason = 'The operator retry for this unresolved checkpoint has already been used.'
        elif value.get('token'):
            reason = 'A verification attempt is still active.'
        elif not job or not job.get('workflow_run_id') or not row.get('task_id') or not _latest_conversation_run(store, job):
            reason = 'Only the current execution request can retry verification.'
        elif row.get('status') not in {'needs_user', 'claimed', 'recovering'}:
            reason = 'This request is no longer waiting for verification.'
        elif policy.get('policy_version') != 2 or policy.get('mode') != 'auto':
            reason = 'The approved mission does not enable automatic verification.'
        elif value.get('state') == 'awaiting_user' and challenge_policy._expired(value, challenge_policy._now()):
            eligible = True
            reason = 'Authorize one new bounded attempt; the previous episode remains in the audit history.'
    return {'eligible': eligible, 'reason': reason, 'max_attempts': ATTEMPTS, 'max_seconds': seconds,
            'previous_attempts': (value or {}).get('attempts', 0),
            'previous_elapsed_seconds': challenge_policy._elapsed(value, challenge_policy._now()) if value and value.get('clock') == 'elapsed' else 0}


def authorize_retry(store, row, *, key, authorizer='operator'):
    """Archive one expired episode and issue one smaller, job-bound allocation."""
    ident = row['challenge_id']
    with store._tx() as conn:
        old = store._doc(conn, 'challenge', ident)
        if not old:
            raise ControlConflict('The challenge episode is unavailable')
        value = deepcopy(old['data'])
        prior_grant = value.get('operator_retry')
        if prior_grant:
            if prior_grant.get('idempotency_key') == key and prior_grant.get('handoff_id') == row['id']:
                return challenge_policy._public(value, challenge_policy._now())
            raise ControlConflict('The unresolved operator retry cannot be renewed')
        if not authorizer or value.get('clock') != 'elapsed' or value.get('token'):
            raise ControlConflict('An inactive elapsed challenge is required')
        now = challenge_policy._now()
        if value.get('state') != 'awaiting_user' or not challenge_policy._expired(value, now):
            raise ControlConflict('Only an expired waiting episode can receive an operator retry')
        job = store._job(conn, row['job_id'])
        policy = challenge_policy.normalize_policy(job['mission'].get('on_challenge'))
        if policy.get('policy_version') != 2 or policy.get('mode') != 'auto':
            raise ControlConflict('The mission requires manual verification')
        seconds = min(SECONDS, float(value['hard_max_elapsed_seconds']), float(policy['hard_max_elapsed_seconds']))
        if seconds <= 0 or min(value['hard_max_attempts'], policy['hard_max_attempts']) < ATTEMPTS:
            raise ControlConflict('The approved challenge ceiling does not admit this allocation')
        archive_key = [ident, value['episode']]
        if store._doc(conn, 'challenge.episode', archive_key):
            raise ControlConflict('The previous episode is already archived; inspect before retrying')
        archive_id = canonical_digest(archive_key)
        owner = store._job(conn, old['job_id'])
        store._put(conn, 'challenge.episode', archive_key, {'id': archive_id,
            'challenge_id': ident, 'episode': value['episode'], 'snapshot': value,
            'snapshot_digest': canonical_digest(value), 'archived_at': now.isoformat(),
            'reason': 'explicit_operator_retry', 'authorized_by': authorizer}, owner)
        grant = {'handoff_id': row['id'], 'idempotency_key': key, 'authorized_by': authorizer,
            'job_id': job['id'], 'task_id': row['task_id'], 'previous_episode_ref': archive_id,
            'allocated_attempts': ATTEMPTS, 'allocated_seconds': seconds, 'authorized_at': now.isoformat()}
        new = {**value, 'episode': value['episode'] + 1, 'state': 'detected', 'attempts': 0,
            'active_seconds': 0., 'max_attempts': ATTEMPTS, 'hard_max_attempts': ATTEMPTS,
            'max_active_seconds': seconds, 'max_elapsed_seconds': seconds, 'hard_max_elapsed_seconds': seconds,
            'adaptive': False, 'mode': 'auto', 'started_at': now.isoformat(),
            'deadline_at': (now + timedelta(seconds=seconds)).isoformat(),
            'hard_deadline_at': (now + timedelta(seconds=seconds)).isoformat(),
            'token': None, 'reserved_at': None, 'expires_at': None, 'reservation_job_id': None,
            'episode_owner_job_id': job['id'], 'episode_owner_revision': job['revision'],
            'episode_owner_generation': job['generation'], 'participant_policies': {job['id']: canonical_digest(policy)},
            'applied_policy_digests': [canonical_digest(policy)], 'policy_initial_attempts': ATTEMPTS,
            'policy_initial_elapsed_seconds': seconds, 'budget_epoch': value.get('budget_epoch', 1) + 1,
            'evidence': None, 'failures': [], 'stop_reason': None, 'last_extension_attempt': -1,
            'operator_retry': grant, 'previous_episode_ref': archive_id}
        for field in ('last_reservation', 'reservation_job_revision', 'reservation_job_generation', 'legacy_active_limit'):
            new.pop(field, None)
        store._put(conn, 'challenge', ident, new, job)
        store._event(conn, job['id'], 'challenge.operator_retry_authorized', {
            'challenge_id': ident, 'episode': new['episode'], 'previous_episode_ref': archive_id,
            'handoff_id': row['id'], 'authorized_by': authorizer, 'max_attempts': ATTEMPTS,
            'max_elapsed_seconds': seconds, 'deadline_at': new['deadline_at']})
        return challenge_policy._public(new, now)


def _current_execution(engine, row):
    if not engine.handoffs.is_current(row):
        raise AccessDenied('This request belongs to an earlier mission revision')
    job = engine.store.get_job(row['job_id'])
    task = engine.store.get_task(row.get('task_id')) if row.get('task_id') else None
    if (not job or not job.get('workflow_run_id') or not _latest_conversation_run(engine.store, job) or not task or task['job_id'] != job['id']
            or task['revision'] != job['revision'] or task['generation'] != job['generation']
            or task['state'] not in {'awaiting_user', 'awaiting_auth', 'paused'}):
        raise AccessDenied('Retry requires the current waiting execution task')
    run = engine.workflows.get_run(job['workflow_run_id'])
    node = next((node for node in engine.workflows.nodes(run['id']) if node['id'] == task['input'].get('node_id')), None)
    if (not run['interrupt_confirmed'] or task['input'].get('run_id') != run['id']
            or not node or node.get('task_id') != task['id']
            or node['status'] not in {'awaiting_user', 'awaiting_auth', 'paused'}):
        raise AccessDenied('The workflow node or interruption state changed')
    return job, task


async def retry_verification(engine, broker, row, body):
    if broker.remote:
        raise AccessDenied('Operator retry currently requires the local coordinator browser')
    job, task = _current_execution(engine, row)
    profile = deepcopy(engine.profile(job['mission']))
    checkpoint = safe_checkpoint(row.get('checkpoint_url'))
    challenge = engine.store.get_challenge(row.get('challenge_id')) if row.get('challenge_id') else None
    origin = f'{urlsplit(checkpoint).scheme}://{urlsplit(checkpoint).netloc}' if checkpoint else None
    if (not challenge or not checkpoint or challenge.get('origin') != origin
            or challenge.get('auth_context') != str(profile.get('id', 'public')) + ':' + str(profile.get('principal_id', 'operator'))
            or row.get('access_profile_ref') != str(profile.get('id', 'public'))):
        raise AccessDenied('The checkpoint, challenge and access profile do not match')
    grant = challenge.get('operator_retry')
    replay = bool(grant and grant.get('handoff_id') == row['id'] and grant.get('idempotency_key') == body['idempotency_key'])
    if not replay and not retry_metadata(engine.store, row)['eligible']:
        raise AccessDenied(retry_metadata(engine.store, row)['reason'])
    sid = row.get('session_id')
    if broker.exists(sid):
        existing = broker.session(sid)
        _session_identity(existing, job, task, profile)
        if getattr(existing.context, 'desktop', False):
            from .desktop import DesktopError
            native = getattr(existing.context.runtime, 'sessions', {}).get(sid)
            stopped = hasattr(existing.context.runtime, 'sessions') and (native is None or native.closed)
            if not stopped:
                try:
                    await engine.browser.observe(sid, owner='human', screenshot=False)
                except DesktopError:
                    stopped = True
            if stopped:
                # Confirm native termination before replacing a stale facade;
                # never spend the new allocation on an already ended desktop.
                await engine.browser.close_session(sid)
    created = None
    if not broker.exists(sid):
        if row.get('browser_transport') != 'desktop_chrome':
            raise AccessDenied('Restore the request browser before authorizing an automatic retry')
        from .native_scope import native_browser_scope
        mission_copy, profile_copy = await native_browser_scope(job['mission'], profile, checkpoint)
        created = await engine.browser.create(job['id'], mission_copy, profile_copy, agent_id='task:' + task['id'])
        created.native_scope_binding = {'checkpoint': checkpoint,
            'mission_digest': canonical_digest(job['mission']), 'profile_digest': canonical_digest(profile)}
        try:
            # Restoring a browser alone cannot renew the exhausted episode.
            created.challenge_id, created.challenge_origin, created.challenge_url = challenge['id'], origin, checkpoint
            engine.handoffs.update(row['id'], {'session_id': created.id, 'session_state': 'live'})
            summary = await engine.browser.takeover(created.id)
            observed = await engine.browser.action(created.id, 'navigate', {'url': checkpoint, 'epoch': summary['epoch']}, owner='human')
            if safe_checkpoint(created.page.url) != checkpoint or observed.get('page_state') == 'error':
                raise AccessDenied('The native checkpoint did not recover sufficiently to retry verification')
            sid = created.id
            for duplicate in engine.handoffs.list(job['id'], 'active'):
                if duplicate['id'] != row['id'] and duplicate.get('session_id') == sid:
                    engine.handoffs.update(duplicate['id'], {'status': 'cancelled',
                        'resolution': 'attached_to_existing_handoff', 'related_handoff_id': row['id']})
        except BaseException:
            await engine.browser.close_session(created.id)
            engine.handoffs.update(row['id'], {'session_id': sid, 'session_state': 'lost'})
            raise
    try:
        session = broker.session(sid)
        _session_identity(session, job, task, profile)
        if sid == row.get('session_id') and body.get('expected_epoch') != session.epoch:
            raise ControlConflict('Browser control changed; reload the request')
        async with session.lock:
            await engine.browser._authorize(session, 'human', session.epoch, 'operator')
            if session.challenge_reservation or getattr(session, 'enrollment_pending', None):
                raise AccessDenied('An attempt or provider form action remains in flight')
            from .browser_resume import _expected_scope
            await _expected_scope(engine, job, session)
            _current_execution(engine, row)
            if engine.profile(job['mission']) != profile:
                raise AccessDenied('The access profile changed before retry authorization')
            current = engine.handoffs.get(row['id'])
            if current.get('inflight') != {'key': body['idempotency_key'], 'action': 'retry_verification'}:
                raise ControlConflict('Retry action ownership changed')
            authorized = authorize_retry(engine.store, row, key=body['idempotency_key'])
            session.challenge_id, session.challenge_origin, session.challenge_url = challenge['id'], origin, checkpoint
            session.challenge_episode = authorized['episode']
        if challenge_policy._expired(authorized, challenge_policy._now()):
            raise ControlConflict('The authorized retry expired; it cannot be renewed')
        # Returning control under this explicit new grant is not a success claim.
        summary = await engine.browser.resume(sid)
        with engine.store._tx() as conn:
            node = engine.store._doc(conn, 'workflow.node', [job['workflow_run_id'], task['input']['node_id']])['data']
            if node.get('task_id') != task['id']:
                raise ControlConflict('The waiting node changed before retry continuation')
            node['continuation_delta'] = {
                'reason': 'operator_retry_authorized', 'session_id': sid,
                'transport': 'desktop_chrome' if getattr(session.context, 'desktop', False) else 'playwright',
                'max_attempts': ATTEMPTS, 'max_seconds': authorized['max_elapsed_seconds'],
                'deadline_at': authorized['deadline_at'],
                'instruction': 'Call state first to acquire the current browser and control epoch. Use this restored request session, not an earlier browser ID or a new browser. One bounded verification attempt was authorized; access has not yet been verified. Prior receipts and mission scope remain unchanged.'}
            engine.workflows._save(conn, 'workflow.node', [job['workflow_run_id'], node['id']], node, job)
        return {'status': 'resolved', 'resolution': 'operator_retry_authorized',
                'session_id': sid, 'session_state': 'live', 'control_epoch': summary['epoch'],
                'browser_transport': 'desktop_chrome' if getattr(session.context, 'desktop', False) else 'playwright',
                'reason': 'One bounded automatic verification attempt was authorized. Previous attempts remain in the audit history.',
                'resume_pending': True,
                'retry_allocation': {'attempts': ATTEMPTS, 'seconds': authorized['max_elapsed_seconds'],
                    'deadline_at': authorized['deadline_at'], 'previous_episode_ref': authorized['previous_episode_ref']}}
    except BaseException:
        if created is not None and not created.closed:
            await engine.browser.close_session(created.id)
            engine.handoffs.update(row['id'], {'session_id': row.get('session_id'), 'session_state': 'lost'})
        raise


def _session_identity(session, job, task, profile):
    if (session.job_id != job['id'] or session.profile_id != profile.get('id', 'public')
            or session.principal_id != profile.get('principal_id', 'operator') or session.agent_id != 'task:' + task['id']):
        raise AccessDenied('The browser does not belong to this waiting task and profile')


async def resume_authorized_retry(engine, row, key):
    """Recover scheduler admission with the same grant and unchanged authority."""
    row = engine.handoffs.get(row['id'])
    if not row.get('resume_pending'):
        return row
    job = engine.store.get_job(row['job_id'])
    challenge = engine.store.get_challenge(row.get('challenge_id'))
    grant = (challenge or {}).get('operator_retry', {})
    if (not engine.handoffs.is_current(row) or not job or not _latest_conversation_run(engine.store, job)
            or grant.get('handoff_id') != row['id'] or grant.get('idempotency_key') != key
            or grant.get('job_id') != job['id'] or grant.get('task_id') != row.get('task_id')):
        raise AccessDenied('The authorized retry no longer belongs to this execution')
    task = engine.store.get_task(row.get('task_id'))
    if (not task or task['job_id'] != job['id'] or task['revision'] != job['revision']
            or task['generation'] != job['generation'] or task['input'].get('run_id') != job.get('workflow_run_id')):
        raise ControlConflict('The authorized retry task changed')
    run = engine.workflows.get_run(job['workflow_run_id'])
    node = engine.store.get_document('workflow.node', [run['id'], task['input']['node_id']])
    if (not node or not run['interrupt_confirmed']
            or node.get('continuation_delta', {}).get('session_id') != row.get('session_id')):
        raise ControlConflict('The authorized retry continuation changed')
    profile = deepcopy(engine.profile(job['mission']))
    session = engine.browser.get(row.get('session_id'))
    if (session.job_id != job['id'] or session.profile_id != profile.get('id', 'public')
            or session.principal_id != profile.get('principal_id', 'operator') or session.control != 'agent'
            or session.agent_id not in {'task:' + task['id'], 'task:' + str(node.get('task_id'))}):
        raise AccessDenied('The retry browser is no longer owned by this execution')
    from .browser_resume import _expected_scope
    await _expected_scope(engine, job, session)
    latest = engine.store.get_job(job['id'])
    if (not engine.handoffs.is_current(row) or latest['mission'] != job['mission']
            or engine.profile(latest['mission']) != profile or not _latest_conversation_run(engine.store, latest)):
        raise AccessDenied('The retry authority changed before execution resumed')
    if any(item.get('session_id') == session.id for item in engine.handoffs.list(job['id'], 'active')):
        raise AccessDenied('The retry browser has another unresolved user request')
    if node['status'] not in {'running', 'succeeded'}:
        if challenge_policy._expired(challenge, challenge_policy._now()):
            raise ControlConflict('The authorized retry expired before execution resumed; it cannot be renewed')
        if node.get('task_id') != task['id'] and node['status'] not in {'pending', 'queued'}:
            raise ControlConflict('The retry node changed before execution was acknowledged')
        # resume() may have scheduled its replacement task before engine.start()
        # failed. Calling it again preserves that task and retries worker startup.
        # A queued node alone is not evidence that startup succeeded.
        await engine.run(job['id'])
    return engine.handoffs.update(row['id'], {'resume_pending': False, 'execution_resumed': True})
