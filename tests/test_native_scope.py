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


def archive_text(year=None):
    # Realistic OCR: the banner is cropped and publisher words are separated;
    # the independently observed Chrome title retains the full site identity.
    if year:
        return f'European Heart Jou\nOxford\nstitution\n{year} issues\nVolume 45, Issue 1, 1 January {year}\nVolume 45, Issue 21, 1 June {year}'
    return 'European Heart Jou\nOxford\nstitution\nAll Issues\nSelect year\n2026\n2025\n2024\n2023\n2022\n2021\n2020\n2019'


@pytest.mark.asyncio
@pytest.mark.parametrize('year', [None, 2024])
async def test_exact_ehj_archive_uses_branded_title_and_specific_body_when_logo_ocr_is_cropped(year):
    from ore.desktop_browser import target_recovered
    from test_desktop_browser import fixture_session
    url = EHJ + '/issue-archive' + (f'/{year}' if year else '')
    copied, profile = native_scope_copy(collection(), {}, url)
    runtime, session = await fixture_session(copied, profile)
    session.challenge_origin, session.challenge_url = 'https://academic.oup.com', url
    label = f'{year} issues' if year else 'All Issues'
    runtime.value.update(url=url, title=f'{label} | European Heart Journal | Oxford Academic - Google Chrome', text=archive_text(year))
    recovered, evidence = await target_recovered(session)
    assert recovered and evidence['target_markers_observed'] == 4
    assert evidence['capture_kind'] == 'desktop_screenshot_ocr' and evidence['http_status_observed'] is False
    assert session.mission['scope']['article_types'] == 'all'
    assert session.mission['publication_window'] == collection()['publication_window']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['wrong_year_body', 'wrong_year_title', 'wrong_path', 'wrong_journal_title',
    'missing_publisher_title', 'title_only', 'no_issue_rows', 'challenge', 'error', 'no_pack_context', 'generic_article'])
async def test_ehj_archive_does_not_accept_branded_title_without_exact_archive_evidence(case):
    from ore.desktop_browser import target_recovered
    from test_desktop_browser import fixture_session
    url = EHJ + '/issue-archive/2024'
    copied, profile = native_scope_copy(collection(), {}, url)
    runtime, session = await fixture_session(copied, profile)
    session.challenge_origin, session.challenge_url = 'https://academic.oup.com', url
    runtime.value.update(url=url, title='2024 issues | European Heart Journal | Oxford Academic - Google Chrome', text=archive_text(2024))
    if case == 'wrong_year_body': runtime.value['text'] = archive_text(2023)
    elif case == 'wrong_year_title': runtime.value['title'] = '2023 issues | European Heart Journal | Oxford Academic'
    elif case == 'wrong_path': runtime.value['url'] = EHJ + '/issue-archive/2023'
    elif case == 'wrong_journal_title': runtime.value['title'] = '2024 issues | Europace | Oxford Academic'
    elif case == 'missing_publisher_title': runtime.value['title'] = '2024 issues | European Heart Journal'
    elif case == 'title_only': runtime.value['text'] = 'A branded navigation header alone supplies no evidence of the requested archive or any issue entries.'
    elif case == 'no_issue_rows': runtime.value['text'] = '2024 issues. European Heart Jou. Oxford stitution. The archive entries have not loaded in this observation.'
    elif case == 'challenge': runtime.value['text'] += '\nVerify you are human'
    elif case == 'error': runtime.value['text'] += '\nERR_CONNECTION_RESET'
    elif case == 'no_pack_context': session.mission.pop('desktop_context')
    elif case == 'generic_article':
        runtime.value['url'] = session.challenge_url = EHJ + '/article/45/21/1'
    assert (await target_recovered(session))[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('configured_in', ['mission', 'profile'])
async def test_ehj_archive_keeps_explicit_stricter_markers_as_additional_requirements(configured_in):
    from ore.desktop_browser import target_recovered
    from test_desktop_browser import fixture_session
    url = EHJ + '/issue-archive/2024'
    mission, profile = collection(), {}
    (mission if configured_in == 'mission' else profile)['desktop_success_text'] = ['Institution access verified']
    copied, access = native_scope_copy(mission, profile, url)
    runtime, session = await fixture_session(copied, access)
    session.challenge_origin, session.challenge_url = 'https://academic.oup.com', url
    runtime.value.update(url=url, title='2024 issues | European Heart Journal | Oxford Academic', text=archive_text(2024))
    assert (await target_recovered(session))[0] is False
    runtime.value['text'] += '\nInstitution access verified'
    assert (await target_recovered(session))[0] is True


def test_all_issues_archive_requires_multiple_year_entries_and_the_exact_official_path():
    from ore.desktop_browser import ehj_archive_checkpoint_observed
    url = EHJ + '/issue-archive'
    title = 'All Issues | European Heart Journal | Oxford Academic'
    assert not ehj_archive_checkpoint_observed(url, url, title, 'All Issues 2024')
    assert not ehj_archive_checkpoint_observed(url, url, title, '2024 2023')
    assert not ehj_archive_checkpoint_observed(url, url + '/2024', title, archive_text())
    assert not ehj_archive_checkpoint_observed('https://academic.oup.com.evil.test/eurheartj/issue-archive',
        'https://academic.oup.com.evil.test/eurheartj/issue-archive', title, archive_text())
