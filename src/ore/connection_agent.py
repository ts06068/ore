"""Trusted scope envelope for optional provider-account setup assistance."""
from copy import deepcopy


def provider_setup_plan(card, handoff, source_mission, backend='codex', model=None):
    mission = deepcopy(source_mission)
    mission.update(backend={'kind': backend}, agent_runtime='auto', conversation_id=card.get('conversation_id'),
                   connection_id=card['id'], artifact_roles=[], sources=[],
                   retrieval_policy={'mode': 'api_open_access_first', 'browser_fallback': True},
                   allowed_capabilities=['state', 'browser_open', 'browser_observe', 'browser_action', 'handoff'],
                   budget={'max_turns': 12, 'max_seconds': 300, 'max_tokens': 250000, 'max_bytes': 10485760})
    mission['operator_access'] = {**mission.get('operator_access', {}), 'purpose': 'provider_setup',
        'form_mutations': 'operator_only'}
    if model:
        mission['model'] = model
    goal = ('Open the selected provider setup page and use the existing account first. '
            'Navigate and inspect ordinary setup forms inside the approved origins. '
            'The host permits navigation, observation, scrolling and waiting only. Form clicks, typing and submission require operator control. '
            'Never invent identity or affiliation. Never copy credentials, cookies, keys or login codes into tool arguments, '
            'messages or outputs. Hand control to the user for authentication, MFA, payment, registration submission, '
            'terms acceptance or missing approved origins. Do not create an account or accept terms yourself. '
            'If API approval is pending, report pending and preserve the request; do not start collection. '
            'Credentials and issued API keys use only the protected connection controls. '
            'Finish with a public status describing the observed blocker or remaining verification, not an inferred entitlement.')
    return {'goal': 'Assist with '+card['provider']+' connection', 'mission': mission,
            'workflow': {'schema_version': 'ore.workflow/v2', 'nodes': [{
                'id': 'provider-setup', 'kind': 'agent', 'goal': goal,
                'inputs': {'starting_url': handoff['checkpoint_url'], 'provider': card['provider'],
                           'operation': card['operation']},
                'allowed_tools': mission['allowed_capabilities'],
                'checks': [{'type': 'schema', 'schema': {'type': 'object',
                    'properties': {'status': {'enum': ['awaiting_user', 'approval_pending', 'needs_verification']}},
                    'required': ['status']}}]}]}}


def assert_setup_action(mission, name, args):
    """Enforce the setup envelope before any local/remote browser dispatch.

    A visual model cannot reliably classify a checkbox or form submit as legal
    assent. This candidate leaves all form mutation to authenticated human
    control; future approvals require a form/action digest bound receipt.
    """
    if mission.get('operator_access', {}).get('purpose') != 'provider_setup':
        return
    from .policy import AccessDenied
    if name not in {'state', 'browser_open', 'browser_observe', 'browser_action', 'handoff', 'finish'}:
        raise AccessDenied('Provider setup permits scoped navigation and inspection only')
    if name == 'browser_action' and args.get('action') not in {'navigate', 'wait', 'scroll', 'back', 'tab'}:
        raise AccessDenied('Provider setup form changes require authenticated operator control')
