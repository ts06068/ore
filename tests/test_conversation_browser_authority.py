"""Host collection authority admits source browser interaction, not account setup."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ore.browser import BrowserManager
from ore.challenge_policy import default_policy
from ore.conversation_authority import request_envelope, bind_request_envelope
from ore.models import Budget
from ore.policy import RateLimiter
from test_browser import site, browser_dependencies
from test_engine import engine
from test_workflow_native import ScriptedSession, agent, drain


def envelope(catalog, profile, *, constraints=None, content='Download the articles using the journal website'):
    message = {'id': 'user-request', 'role': 'user', 'content': content}
    record = {'id': 'collection-chat', 'messages': [message], 'settings': {
        'mode': 'execute', 'execution_policy': 'auto_within_scope', 'constraints': constraints or {}}}
    return request_envelope(record, message, message['id'], catalog, profile)


def test_collection_browser_authority_preserves_explicit_capability_limits(engine):
    catalog = engine.capabilities.catalog()
    profile = engine.profile({})
    ordinary = envelope(catalog, profile)
    assert {'browser_open', 'browser_observe', 'browser_action', 'challenge'} <= set(ordinary['allowed_tools'])
    assert 'provider_form' not in ordinary['allowed_tools']
    assert not ordinary['requires_connection_authority']
    for key in ('allowed_tools', 'capabilities'):
        narrowed = envelope(catalog, profile, constraints={key: ['browser_open', 'browser_observe', 'provider_form']})
        assert set(narrowed['allowed_tools']) == {'browser_open', 'browser_observe'}
    assert envelope(catalog, profile, content='Create an account and download the journal articles')['requires_connection_authority']


@pytest.mark.browser
async def test_auto_collection_envelope_allows_reserved_browser_action_through_recipe(engine, site, browser_dependencies, monkeypatch):
    monkeypatch.setattr('ore.workflow_native.AgentSession', ScriptedSession)
    engine.backend = SimpleNamespace(sessions=[], prompts=[], rounds={}, results=[], usage_totals={}, timeouts=[], close=AsyncMock())
    engine.models = AsyncMock(return_value=[{'id': 'fixture'}])
    engine.routing_for = lambda job, kind, failures, catalog: {'model': 'fixture', 'effort': 'high', 'mode': 'fixed'}
    engine.start = AsyncMock()
    profile = {'id': 'public', 'allow_private_network': True, 'sources': {}}
    engine.save_profile(profile)
    engine.browser = BrowserManager(engine.settings, RateLimiter(), store=engine.store, secrets=engine.secrets, on_event=engine.event)
    authority = envelope(engine.capabilities.catalog(), profile, constraints={'allowed_origins': [site]})
    proposed = {'goal': authority['goal'], 'budget': Budget().model_dump(), 'constraints': {},
        'workflow': {'nodes': [agent()]}, 'mission': {'goal': authority['goal'], 'artifact_roles': [],
        'allowed_origins': [site], 'on_challenge': default_policy(), 'limits': {'origin_min_interval_seconds': 0}}}
    assert bind_request_envelope(proposed, authority, profile) == []
    consequential = deepcopy(authority)
    consequential['requires_connection_authority'] = True
    assert bind_request_envelope(deepcopy(proposed), consequential, profile) == ['connection_action_requires_authority']
    assert 'provider_form' not in proposed['constraints']['allowed_tools']
    async def script(session, prompt):
        opened = await session.call('browser_open', {'url': site + '/challenge'})
        sid = opened['session_id']
        reserved = await session.call('challenge', {'session_id': sid})
        assert reserved['allowed']
        result = await session.call('recipe.execute', {'inputs': {}, 'program': {'version': 1,
            'steps': [{'id': 'verify', 'tool': 'browser_action', 'inputs': {
                'session_id': sid, 'epoch': engine.browser.get(sid).epoch, 'action': 'click', 'selector': '#solve'}}],
            'output': {'recovered': {'$ref': 'steps.verify.challenge_detected'}}}})
        assert not result.get('error'), result
        assert engine.store.get_challenge(engine.browser.get(sid).challenge_id)['state'] == 'resolved'
        await session.call('workflow.finish', {'output': {'done': True}})
    engine.backend.script = script
    run = engine.workflows.create_run(proposed)
    await engine.workflows.start_run(run['id'])
    assert (await drain(engine, run))['status'] == 'completed'
