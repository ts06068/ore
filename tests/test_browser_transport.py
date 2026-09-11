"""Explicit native transport preserves source and browser ownership contracts."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ore.contracts import validate_action
from ore.models import canonical_digest
from ore.policy import AccessDenied
from ore.tools import ToolRuntime
from test_engine import engine, mission


def browser_stub():
    async def create(job_id, mission, profile, *, agent_id=None):
        return SimpleNamespace(id='chosen-browser', job_id=job_id, mission=mission,
            policy=SimpleNamespace(profile=profile), context=SimpleNamespace(desktop=profile.get('browser_backend') == 'desktop_chrome'))
    return SimpleNamespace(create=AsyncMock(side_effect=create),
        action=AsyncMock(return_value={'session_id': 'chosen-browser', 'control': 'agent', 'challenge_detected': False}),
        observe=AsyncMock(return_value={'session_id': 'chosen-browser', 'control': 'agent'}), close=AsyncMock())


def test_browser_transport_has_closed_optional_contract():
    assert validate_action('browser_open', {}) == {}
    for transport in ('desktop_chrome', 'playwright'):
        assert validate_action('browser_open', {'transport': transport}) == {'transport': transport}
    with pytest.raises(ValidationError):
        validate_action('browser_open', {'transport': 'arbitrary-browser'})


async def test_native_browser_open_derives_only_checkpoint_scope_without_changing_job(engine):
    engine.save_profile({'id': 'public', 'allow_private_network': True, 'sources': {}})
    job = engine.create(mission(allowed_origins=[], scope={'article_types': 'all', 'journal_id': 'fixture'},
        publication_window={'basis': 'issue_date', 'from': '2024-06-01', 'until_exclusive': '2024-07-01'}), queued=False)
    before = deepcopy(job['mission'])
    before_profile = deepcopy(engine.profile(before))
    engine.browser = browser_stub()
    result = await ToolRuntime(engine, job['id']).execute('browser_open', {
        'url': 'http://127.0.0.1/article', 'transport': 'desktop_chrome'})
    assert result['session_id'] == 'chosen-browser'
    selected = engine.browser.create.await_args
    assert selected.args[0] == job['id'] and selected.args[1]['allowed_origins'] == ['http://127.0.0.1']
    assert selected.args[1]['scope'] == before['scope']
    assert selected.args[1]['publication_window'] == before['publication_window']
    assert selected.args[2] == {**before_profile, 'browser_backend': 'desktop_chrome', 'require_desktop': True}
    assert engine.store.get_job(job['id'])['mission'] == before and engine.profile(before) == before_profile
    engine.browser.action.assert_awaited_once_with('chosen-browser', 'navigate', {'url': 'http://127.0.0.1/article'}, owner_id=None)


@pytest.mark.parametrize('restriction', ['require_companion', 'selected_companion', 'paired_companion', 'require_desktop'])
async def test_explicit_transport_cannot_replace_required_profile_or_companion(engine, restriction):
    profile = {'id': 'public', 'allow_private_network': True, 'sources': {}}
    if restriction in {'require_companion', 'require_desktop'}: profile[restriction] = True
    engine.save_profile(profile)
    job = engine.create(mission(), queued=False)
    engine.browser = browser_stub()
    if restriction.endswith('_companion') and restriction != 'require_companion':
        engine.browser.companion_hub = SimpleNamespace(selected=lambda ident: restriction == 'selected_companion',
            available=lambda ident: {'id': 'paired'} if restriction == 'paired_companion' else None)
    transport = 'playwright' if restriction == 'require_desktop' else 'desktop_chrome'
    with pytest.raises(AccessDenied):
        await ToolRuntime(engine, job['id']).execute('browser_open', {'url': 'http://127.0.0.1/article', 'transport': transport})
    engine.browser.create.assert_not_awaited()


async def test_native_transport_requires_admitted_url_before_creation(engine):
    engine.save_profile({'id': 'public', 'allow_private_network': True, 'sources': {}})
    job = engine.create(mission(allowed_origins=['http://127.0.0.1']), queued=False)
    engine.browser = browser_stub()
    for args in ({'transport': 'desktop_chrome'}, {'transport': 'desktop_chrome', 'url': 'https://example.org/outside'}):
        with pytest.raises(AccessDenied):
            await ToolRuntime(engine, job['id']).execute('browser_open', args)
    engine.browser.create.assert_not_awaited()


async def test_omitted_transport_preserves_profile_default(engine):
    job = engine.create(mission(), queued=False)
    engine.browser = browser_stub()
    await ToolRuntime(engine, job['id']).execute('browser_open', {})
    args = engine.browser.create.await_args.args
    assert args[1] == job['mission'] and args[2] == engine.profile(job['mission'])
    engine.browser.observe.assert_awaited_once_with('chosen-browser', owner_id=None)
