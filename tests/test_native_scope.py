"""Native journal adaptation is a bounded copy, not new collection authority."""
from copy import deepcopy
import socket

import pytest

from ore.desktop_api import _session_policy
from ore.native_scope import journal_browser_context, native_browser_scope, native_scope_copy, native_target_checkpoint
from ore.policy import AccessDenied
from ore_scholarly.packs import load_rune

EHJ = 'https://academic.oup.com/eurheartj'
ISSUE = EHJ + '/issue/45/21'
FRAME = 'https://challenges.cloudflare.com'


@pytest.fixture
def public_dns(monkeypatch):
    calls = []
    def resolve(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    return calls


def collection():
    return {'goal': "Download all articles in EHJ's June 2024 issues", 'allowed_origins': [],
            'urls': [EHJ], 'artifact_roles': ['main_pdf', 'supplement', 'full_text'],
            'completeness': 'systematic', 'publication_window': {'from': '2024-06-01', 'until_exclusive': '2024-07-01'},
            'scope': {'journal_id': 'ehj', 'article_types': 'all', 'exclude_related_journals': True},
            'on_challenge': {'max_attempts_per_episode': 3, 'max_elapsed_seconds': 120},
            'source_policy': {'allow': {'browser': ['*'], 'download': ['*']}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('url', [EHJ, EHJ + '/', ISSUE])
async def test_exact_ehj_checkpoint_adds_packaged_frame_scope_only_to_session_copy(public_dns, url):
    mission = collection();profile = {'id': 'institution', 'principal_id': 'researcher'}
    original_mission, original_profile = deepcopy(mission), deepcopy(profile)
    copied, access = await native_browser_scope(mission, profile, url)
    assert copied['allowed_origins'] == ['https://academic.oup.com', FRAME]
    assert 'brunhild.challenges.cloudflare.com' not in copied['allowed_origins']
    context = copied['desktop_context'];pack = load_rune('ehj')
    assert context == {'protocol_id': pack['protocol_id'], 'digest': pack['digest'], 'scope': 'browser_session_only'}
    for key in ('scope', 'artifact_roles', 'completeness', 'publication_window', 'source_policy', 'on_challenge'):
        assert copied[key] == original_mission[key]
    assert copied['scope']['article_types'] == 'all'
    assert access['id'] == profile['id'] and access['principal_id'] == profile['principal_id']
    assert access['require_desktop'] is True and access['browser_backend'] == 'desktop_chrome'
    assert mission == original_mission and profile == original_profile
    assert set(public_dns) == {'academic.oup.com', 'challenges.cloudflare.com'}
    if '/issue/' in url:
        assert copied['desktop_issue_checkpoint'] == ISSUE
        assert 'desktop_success_text' not in copied
    else:
        assert copied['desktop_success_text'] == ['European Heart Journal', 'Oxford Academic']
        assert 'desktop_issue_checkpoint' not in copied


@pytest.mark.parametrize('url', [
    'https://academic.oup.com/europace', 'https://academic.oup.com/eurheartj-cardiovascimaging',
    'https://academic.oup.com/eurheartjopen/issue/1/1', 'https://academic.oup.com/eurheartjournal',
    'https://academic.oup.com.evil.test/eurheartj', 'http://academic.oup.com/eurheartj',
    'https://academic.oup.com:8443/eurheartj', 'https://user:secret@academic.oup.com/eurheartj',
    'https://academic.oup.com/eurheartj/../europace', 'https://academic.oup.com/eurheartj/%2e%2e/europace',
    'https://academic.oup.com/eurheartj/%5c..%5ceuropace',
    'https://[broken', 'https://academic.oup.com:invalid/eurheartj', None,
])
def test_other_journals_lookalikes_and_invalid_urls_do_not_receive_ehj_context(url):
    assert journal_browser_context(url) == {}


@pytest.mark.asyncio
async def test_related_oup_journal_does_not_inherit_ehj_dependencies_or_markers(public_dns):
    url = 'https://academic.oup.com/europace'
    copied, _ = await native_browser_scope({'goal': 'Read requested page', 'urls': [url]}, {}, url)
    assert copied['allowed_origins'] == ['https://academic.oup.com']
    assert 'desktop_context' not in copied and 'desktop_success_text' not in copied
    assert 'desktop_issue_checkpoint' not in copied
    assert set(public_dns) == {'academic.oup.com'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mission_change,profile,reason', [
    ({'allowed_origins': ['https://other.test']}, {}, 'Origin outside the mission'),
    ({}, {'origins': ['https://academic.oup.com']}, 'Origin outside access profile'),
    ({}, {'source_policy': {'exclude': {'browser': ['ehj']}}}, 'source excluded'),
    ({}, {'source_policy': {'exclude': {'download': ['ehj']}}}, 'source excluded'),
    ({}, {'source_policy': {'exclude': {'browser': ['general_web']}}}, 'source excluded'),
    ({}, {'require_companion': True}, 'requires the companion'),
    ({'scope': {'allowed_paths': ['/eurheartj']}}, {}, 'path-restricted'),
])
async def test_packaged_frame_dependencies_preserve_explicit_access_constraints(public_dns, mission_change, profile, reason):
    mission = {**collection(), **mission_change};before = deepcopy((mission, profile))
    with pytest.raises(AccessDenied, match=reason):
        await native_browser_scope(mission, profile, ISSUE)
    assert (mission, profile) == before


def test_operator_attachment_uses_same_ehj_context_without_replacing_collection_criteria():
    mission = collection();mission['allowed_origins'] = ['https://academic.oup.com']
    mission['retrieval_policy'] = {'mode': 'api_open_access_first', 'browser_fallback': False}
    profile = {'id': 'institution', 'principal_id': 'researcher', 'require_companion': True,
               'retrieval_policy': {'mode': 'api_open_access_first', 'browser_fallback': False}}
    before = deepcopy((mission, profile))
    copied, access, additions = _session_policy(mission, profile, ISSUE)
    assert additions == [FRAME]
    assert copied['desktop_issue_checkpoint'] == ISSUE
    assert copied['retrieval_policy']['browser_fallback'] is True
    assert access['retrieval_policy']['browser_fallback'] is True
    assert not access.get('require_companion')
    assert copied['scope']['article_types'] == 'all'
    assert copied['on_challenge'] == mission['on_challenge']
    assert (mission, profile) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('markers_from', ['mission', 'profile'])
async def test_issue_recovery_requires_volume_and_issue_even_with_homepage_markers(markers_from):
    from ore.desktop_browser import target_recovered
    from test_desktop_browser import fixture_session
    copied, profile = native_scope_copy(collection(), {}, EHJ)
    if markers_from == 'profile':
        profile['desktop_success_text'] = copied.pop('desktop_success_text')
    native_target_checkpoint(copied, profile, ISSUE)
    runtime, session = await fixture_session(copied, profile)
    session.challenge_origin = 'https://academic.oup.com';session.challenge_url = ISSUE
    runtime.value.update(url=ISSUE, title='European Heart Journal', text='European Heart Journal. Oxford Academic.')
    recovered, _ = await target_recovered(session)
    assert recovered is False
    runtime.value['text'] = 'European Heart Journal. Oxford Academic. Volume 45, Issue 21.'
    recovered, evidence = await target_recovered(session)
    assert recovered is True and evidence['target_markers_observed'] == 3
    runtime.value['text'] = 'European Heart Journal. Oxford Academic. Volume 45, Issue 22.'
    assert (await target_recovered(session))[0] is False
    assert session.mission['scope']['article_types'] == 'all'
