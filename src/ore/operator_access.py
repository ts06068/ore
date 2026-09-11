"""Finite browser scope for explicit operator navigation and provider setup."""
from __future__ import annotations
from copy import deepcopy
from urllib.parse import urlsplit
from .handoffs import safe_checkpoint
from .policy import AccessDenied


def checkpoint_origin(url):
    try:
        safe = safe_checkpoint(url)
        value = urlsplit(safe or '')
        if not safe or not value.hostname or '*' in value.netloc or value.port is not None and not 0 < value.port < 65536:
            raise ValueError()
        if any(ord(char) < 32 for char in url):
            raise ValueError()
        return f'{value.scheme}://{value.netloc}'
    except (TypeError, ValueError):
        raise AccessDenied('Choose a valid HTTP(S) starting address without embedded credentials') from None


def operator_mission(url, *, profile='public', goal='Operator access onboarding', conversation_id=None):
    origin = checkpoint_origin(url)
    result = {'goal': goal, 'artifact_roles': [], 'sources': [], 'access_profile_ref': profile,
              'urls': [safe_checkpoint(url)], 'allowed_origins': [origin],
              'operator_access': {'purpose': 'account_setup', 'starting_origin': origin}}
    if conversation_id:
        result['conversation_id'] = conversation_id
    return result


def attachment_mission(job, handoff):
    """Narrow a legacy manual session, never widen a collection's contract.

    Only the already persisted checkpoint is eligible. The attach request cannot
    supply another URL, and a workflow/task or retrieval mission cannot use this
    compatibility path. The returned copy applies solely to operator interaction.
    """
    mission = deepcopy(job['mission'])
    existing = mission.get('allowed_origins') or (mission.get('scope') or {}).get('origins')
    if existing:
        return mission, 'mission'
    manual = (not job.get('workflow_run_id') and not job.get('rune') and not handoff.get('task_id')
              and not mission.get('sources') and not mission.get('artifact_roles')
              and handoff.get('kind') in {'source_setup', 'browser', 'challenge', 'authentication'})
    if not manual:
        raise AccessDenied('Desktop attachment requires explicit mission origins; define the collection scope first')
    origin = checkpoint_origin(handoff.get('checkpoint_url'))
    mission['allowed_origins'] = [origin]
    mission['operator_session_scope'] = {'basis': 'persisted_manual_checkpoint', 'origin': origin,
                                         'handoff_id': handoff['id']}
    return mission, 'persisted_manual_checkpoint'
