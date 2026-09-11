"""A hidden publisher sign-in dialog does not mean an article is inaccessible."""
import pytest
from test_browser import browser_dependencies, make_manager, site

@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize('visible_login', [False, True])
async def test_recovery_uses_visible_authentication_gate(tmp_path, site, browser_dependencies, visible_login):
    manager, store, _, job, mission = make_manager(tmp_path, site)
    mission['on_challenge'] = {'max_attempts_per_episode': 1, 'max_active_seconds': 3, 'recovery_settle_seconds': 0.3}
    try:
        session = await manager.create(job['id'], mission, {'id': 'public', 'allow_private_network': True})
        await manager.action(session.id, 'navigate', {'url': site + '/challenge', 'screenshot': False})
        # Publishers often keep an unused login modal in the article DOM.
        await session.page.evaluate('visible => { const form=document.createElement("form"); form.hidden=!visible; const input=document.createElement("input"); input.type="password"; form.append(input); document.body.append(form); }', visible_login)
        await manager.challenge(session.id)
        result = await manager.action(session.id, 'click', {'epoch': session.epoch, 'selector': '#solve', 'screenshot': False})
        record = store.get_challenge(session.challenge_id)
        assert not await manager._challenge_visible(session)
        recovered, evidence = await manager._target_recovered(session)
        assert recovered is (not visible_login)
        if visible_login:
            assert evidence['reason'] == 'authentication_required'
        assert record['state'] == ('awaiting_user' if visible_login else 'resolved')
        assert result['control'] == ('human' if visible_login else 'agent')
        assert record['attempts'] == 1
    finally:
        await manager.close()
        store.close()
