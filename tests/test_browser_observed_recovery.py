"""Content observation settles only a verified, still-current challenge episode."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from playwright.async_api import Error as PlaywrightError

from ore.browser import BrowserManager
from ore.challenge_policy import default_policy
from ore.config import Settings
from ore.policy import RateLimiter
from ore.store import Store
from test_desktop_integration import InteractiveDesktop

URL = 'https://journal.test/article'


@pytest.fixture
async def native(tmp_path, monkeypatch):
    runtime = InteractiveDesktop()
    monkeypatch.setattr('ore.desktop.DesktopRuntime', lambda *args, **kwargs: runtime)
    settings = Settings(state_dir=tmp_path, browser_backend='desktop_chrome').prepare()
    store = Store(settings.database_url)
    store.initialize()
    manager = BrowserManager(settings, RateLimiter(), store=store)
    mission = {'goal': 'Read the requested article', 'allowed_origins': ['https://journal.test', 'https://other.test'],
        'limits': {'origin_min_interval_seconds': 0},
        'on_challenge': {**default_policy(), 'recovery_settle_seconds': .01}}
    profile = {'id': 'fixture', 'allow_private_network': True, 'desktop_success_text': ['Independent target article']}
    job = store.create_job(mission)
    session = await manager.create(job['id'], mission, profile)
    await manager.action(session.id, 'navigate', {'url': URL, 'screenshot': False})
    assert session.challenge_id and session.challenge_episode == 1
    try:
        yield manager, store, session, runtime, job
    finally:
        await manager.close()
        store.close()


def content(runtime, *, url=URL, text='Independent target article now visible with substantive study results.'):
    runtime.value.update(url=url, title='Independent target', text=text)


async def test_transient_action_recovery_is_finalized_by_following_content_observation(native, monkeypatch):
    manager, store, session, runtime, job = native
    original = deepcopy(store.get_challenge(session.challenge_id))
    target = manager._target_recovered
    calls = 0
    async def transition_then_target(current):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PlaywrightError('Navigation replaced the observed page')
        return await target(current)
    monkeypatch.setattr(manager, '_target_recovered', transition_then_target)
    reserved = await manager.challenge(session.id)
    assert reserved['allowed']
    result = await manager.action(session.id, 'click', {'epoch': session.epoch, 'x': 200, 'y': 250, 'screenshot': False})
    final = store.get_challenge(session.challenge_id)
    assert result['challenge_recovery']['resolved'] and result['challenge']['state'] == 'resolved'
    assert final['state'] == 'resolved' and final['attempts'] == 1 and not final['token']
    assert final['deadline_at'] == original['deadline_at'] and final['hard_deadline_at'] == original['hard_deadline_at']
    assert final['evidence']['screenshot_sha256'] and final['evidence']['substantive_content_observed']
    assert session.challenge_reservation is None and session.challenge_episode is None and session.control == 'agent'
    events = store.events(job['id'])
    assert any(row['type'] == 'challenge.finished' and row['payload']['state'] == 'detected' for row in events)
    assert any(row['type'] == 'challenge.resolved' and row['payload']['via'] == 'browser_content_observation' for row in events)
    assert not await manager._challenge_deadline_expired(session)


@pytest.mark.parametrize('case', ['missing_markers', 'wrong_markers', 'different_page', 'different_origin', 'expired', 'active_reservation', 'new_episode'])
async def test_content_alone_cannot_resolve_an_unverified_or_stale_challenge(native, case):
    manager, store, session, runtime, job = native
    content(runtime)
    if case == 'missing_markers':
        session.policy.profile.pop('desktop_success_text')
    elif case == 'wrong_markers':
        content(runtime, text='Different unrelated article with enough substantive text but no expected title marker.')
    elif case == 'different_page':
        content(runtime, url='https://journal.test/different-article')
    elif case == 'different_origin':
        content(runtime, url='https://other.test/article')
    elif case == 'expired':
        with store._tx() as conn:
            row = store._doc(conn, 'challenge', session.challenge_id)
            value = row['data']
            value['deadline_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            store._put(conn, 'challenge', session.challenge_id, value, store._job(conn, job['id']))
    elif case == 'active_reservation':
        assert store.reserve_challenge(job['id'], session.challenge_origin, 'fixture:operator')['allowed']
    elif case == 'new_episode':
        store.resolve_challenge(session.challenge_id, {'operator_verified': True})
        store.observe_challenge(job['id'], session.challenge_origin, 'fixture:operator')
    before = deepcopy(store.get_challenge(session.challenge_id))
    result = await manager.observe(session.id, screenshot=False)
    assert not result['challenge_detected']
    assert store.get_challenge(session.challenge_id) == before
    assert not result.get('challenge_recovery', {}).get('resolved')


@pytest.mark.parametrize('change', ['new_episode', 'new_reservation'])
async def test_recovery_rechecks_episode_and_reservation_after_awaited_observation(native, monkeypatch, change):
    manager, store, session, runtime, job = native
    content(runtime)
    target = manager._target_recovered
    changed = None
    async def concurrently_changed(current):
        nonlocal changed
        if change == 'new_episode':
            store.resolve_challenge(session.challenge_id, {'operator_verified': True})
            store.observe_challenge(job['id'], session.challenge_origin, 'fixture:operator')
        else:
            assert store.reserve_challenge(job['id'], session.challenge_origin, 'fixture:operator')['allowed']
        changed = deepcopy(store.get_challenge(session.challenge_id))
        return await target(current)
    monkeypatch.setattr(manager, '_target_recovered', concurrently_changed)
    result = await manager.observe(session.id, screenshot=False)
    assert changed is not None and store.get_challenge(session.challenge_id) == changed
    assert not result.get('challenge_recovery', {}).get('resolved')


async def test_navigation_cannot_reuse_a_permissive_success_selector_for_an_old_checkpoint(native, monkeypatch):
    from unittest.mock import AsyncMock
    manager, store, session, runtime, job = native
    content(runtime, url='https://journal.test/different-article')
    permissive = AsyncMock(return_value=(True, {'success_selector': 'main'}))
    monkeypatch.setattr(manager, '_target_recovered', permissive)
    before = deepcopy(store.get_challenge(session.challenge_id))
    await manager.observe(session.id, screenshot=False)
    permissive.assert_not_awaited()
    assert store.get_challenge(session.challenge_id) == before
