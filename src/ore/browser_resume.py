"""Fenced browser continuity for a resumed node within one workflow job."""
from __future__ import annotations

from .policy import AccessDenied


async def _expected_scope(engine, job, session):
    """Allow only the exact mission or its committed operator desktop attachment."""
    mission, profile = job['mission'], engine.profile(job['mission'])
    if session.mission == mission and session.policy.profile == profile:
        return mission, profile
    from .models import canonical_digest
    binding = getattr(session, 'native_scope_binding', None)
    if binding and getattr(session.context, 'desktop', False):
        if (binding.get('mission_digest') != canonical_digest(mission)
                or binding.get('profile_digest') != canonical_digest(profile)):
            raise AccessDenied('The original native browser authority changed')
        from .native_scope import native_browser_scope
        copied, native_profile = await native_browser_scope(mission, profile, binding.get('checkpoint'))
        if copied == session.mission and native_profile == session.policy.profile:
            return copied, native_profile
    binding = getattr(session, 'transport_profile_binding', None)
    if (binding and binding.get('transport') == 'playwright'
            and binding.get('mission_digest') == canonical_digest(mission)
            and binding.get('profile_digest') == canonical_digest(profile)
            and not profile.get('require_desktop') and not profile.get('require_companion')
            and session.mission == mission and session.policy.profile == {**profile, 'browser_backend': 'playwright'}):
        return mission, session.policy.profile
    if getattr(session.context, 'desktop', False):
        from .desktop_api import _session_policy
        from .desktop_browser import issue_checkpoint_identity
        from .operator_access import attachment_mission
        for handoff in engine.handoffs.list(job['id']):
            if (handoff.get('session_id') != session.id or not handoff.get('desktop_attachment')
                    or not engine.handoffs.is_current(handoff)):
                continue
            copied, _ = attachment_mission(job, handoff)
            checkpoint = handoff.get('checkpoint_url')
            copied, native_profile, _ = _session_policy(copied, profile, checkpoint)
            if issue_checkpoint_identity(checkpoint):
                copied['desktop_issue_checkpoint'] = checkpoint
            if copied == session.mission and native_profile == session.policy.profile:
                return copied, native_profile
    raise AccessDenied('Browser mission or access profile changed; this session cannot be reclaimed')


def _ownership(runtime, session):
    store = runtime.engine.store
    if (not runtime.task or runtime.task.get('kind') != 'workflow'
            or session.job_id != runtime.job_id or not session.agent_id.startswith('task:')):
        raise AccessDenied('Browser does not belong to this workflow job')
    old = store.get_task(session.agent_id.removeprefix('task:'))
    new = store.get_task(runtime.task['id'])
    job = store.get_job(runtime.job_id)
    if (not old or not new or not job or old.get('kind') != 'workflow'
            or old['job_id'] != new['job_id'] or old['job_id'] != session.job_id
            or old['state'] not in {'awaiting_user', 'awaiting_auth', 'paused', 'paused_budget'}
            or (old['revision'], old['generation']) != (new['revision'], new['generation'])
            or (new['revision'], new['generation']) != (job['revision'], job['generation'])
            or old['input'].get('run_id') != new['input'].get('run_id')
            or old['input'].get('node_id') != new['input'].get('node_id')
            or new['input'].get('node_epoch', 0) <= old['input'].get('node_epoch', 0)):
        raise AccessDenied('Browser cannot transfer to a different, superseded or active workflow task')
    store.validate_task_lease(new['id'], runtime.task['worker_id'], runtime.task['fence'], runtime.task['revision'])
    run = store.get_document('workflow.run', new['input']['run_id'])
    node = store.get_document('workflow.node', [new['input']['run_id'], new['input']['node_id']])
    if (not run or not node or run['status'] != 'running' or node['status'] != 'running'
            or node.get('task_id') != new['id'] or node['control_epoch'] != new['input']['node_epoch']
            or run['control_epoch'] != new['input']['run_epoch']
            or job.get('workflow_run_id') != run['id']):
        raise AccessDenied('Only the current fenced node attempt may reclaim its browser')
    if any(item.get('status') == 'unconfirmed' and item.get('node_id') == node['id']
           for item in run.get('interruptions', [])):
        raise AccessDenied('Previous browser task interruption is not confirmed')
    return old, new, job


async def rebind_workflow_browser(runtime, session, *, strict=True):
    """Transfer only a settled node's agent-owned browser to its new leased task.

    Human ownership, pending form approvals, challenge reservations, profile and
    mission bounds remain authoritative. Discovery skips unrelated sessions;
    explicit operations receive an actionable ownership error.
    """
    if session.agent_id == runtime.actor_id:
        return True
    if not runtime.task or runtime.task.get('kind') != 'workflow':
        return False
    try:
        async with session.lock:
            if session.agent_id == runtime.actor_id:
                return True
            if session.closed or session.closing or session.control != 'agent':
                raise AccessDenied('The browser is closed or still controlled by the operator')
            if getattr(session, 'enrollment_pending', None) or session.challenge_reservation:
                raise AccessDenied('The browser has an unresolved form action or challenge attempt')
            old, new, job = _ownership(runtime, session)
            from copy import deepcopy
            original_profile = deepcopy(runtime.engine.profile(job['mission']))
            _, expected_profile = await _expected_scope(runtime.engine, job, session)
            if (session.profile_id != str(expected_profile.get('id', 'public'))
                    or session.principal_id != str(expected_profile.get('principal_id', 'operator'))):
                raise AccessDenied('Browser principal does not match the current access profile')
            if any(row.get('session_id') == session.id for row in runtime.engine.handoffs.list(job['id'], 'active')):
                raise AccessDenied('The browser still has an unresolved user request')
            if session.challenge_id:
                challenge = runtime.engine.store.get_challenge(session.challenge_id)
                if challenge and challenge.get('state') == 'awaiting_user':
                    raise AccessDenied('The browser challenge still requires access verification')
            # Native scope admission may await DNS. Revalidate the current task
            # and profile after that await before changing any ownership.
            old, new, current_job = _ownership(runtime, session)
            if current_job['mission'] != job['mission'] or runtime.engine.profile(current_job['mission']) != original_profile:
                raise AccessDenied('Browser authority changed while checking recovery')
            # The policy is unchanged. The lease/control checks and transfer are
            # synchronous under the session lock so no input can race this epoch.
            store = runtime.engine.store
            store.validate_control(job['id'], session.id, session.agent_id, session.epoch)
            previous_actor = session.agent_id
            released = store.release_control(job['id'], session.id, previous_actor, session.epoch)
            acquired = store.acquire_control(job['id'], session.id, runtime.actor_id, 'agent', released['epoch'], 3600)
            session.agent_id, session.epoch = runtime.actor_id, acquired['epoch']
            session.elements = []
            await runtime.engine.browser._publish_control(session)
            runtime.engine.event(job['id'], 'browser.task_rebound', {
                'session_id': session.id, 'from_task_id': old['id'], 'task_id': new['id'],
                'node_id': new['input']['node_id'], 'run_id': new['input']['run_id'], 'epoch': session.epoch})
            return True
    except AccessDenied:
        if strict:
            raise
        return False
