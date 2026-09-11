"""Browser acceptance for the real built app, with explicitly labelled display fixtures.

No collection is performed. A real backend is used for login and operational routes;
only the design-fixture conversation's read responses are intercepted for rich states.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', default='http://127.0.0.1:8767')
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('.ore/reports/ui-v04'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    token = args.token_file.read_text().strip()
    checks, errors, screenshots = [], [], []
    conversation = {
        'id': 'design-fixture', 'title': 'Design fixture · June archive', 'status': 'paused',
        'planner_running': False, 'active_plan_id': 'p2', 'approved_plan_id': 'p2',
        'messages': [
            {'id': 'u1', 'role': 'user', 'content': 'Find the original articles in JACC’s June 2024 issues. Include the full text and all supplementary files.'},
            {'id': 'a1', 'role': 'assistant', 'operator_message_id': 'u1', 'content': 'I’ll establish the issue inventory from the journal, then reconcile the original research articles and their declared files.\n\n**The completion criterion:** every included article must have its required files verified, or an explicit unresolved reason.', 'plan_id': 'p1'},
            {'id': 'u2', 'role': 'user', 'content': 'Keep the collected files and pause while I review the remaining items.'},
            {'id': 'a2', 'role': 'assistant', 'operator_message_id': 'u2', 'content': 'The work is paused. Previously collected outputs remain available.\n\nThis screen uses a **design fixture** to show the interface; it is not evidence of journal collection.'}],
        'plans': [{'id': 'p1', 'revision': 1, 'status': 'superseded', 'operator_message_id': 'u1', 'goal': 'Establish the official issue inventory', 'scope': ['JACC · June 2024'], 'outputs': ['Original articles and declared supplements'], 'acceptance': ['Reconcile the official inventory'], 'workflow': {'nodes': [{'id': 'inventory', 'goal': 'Read the official archive'}]}}],
        'runs': [{'id': 'r1', 'operator_message_id': 'u1', 'plan_id': 'p1', 'status': 'paused',
            'nodes': [{'id': 'inventory', 'status': 'succeeded', 'goal': 'Confirm the issue inventory'}, {'id': 'collect', 'status': 'paused', 'goal': 'Retrieve and verify declared files'}],
            'progress': {'stage': 'paused', 'planned_total_known': False},
            'collection_progress': {'denominator': {'status': 'provisional', 'basis': 'Design fixture · issue and attachment declarations are still being reconciled.', 'total': None},
            'resources': {'discovered': 32, 'included': 18, 'complete': 12, 'unresolved': 6},
            'roles': [{'role': 'main_pdf', 'expected': 18, 'verified': 14, 'missing': 4, 'unknown': 0}, {'role': 'supplement', 'expected': None, 'verified': 9, 'missing': 2, 'unknown': 4}],
            'issues': {'discovered': 4, 'complete': 3, 'total': None}, 'gaps': {'count': 6, 'by_kind': {'access_required': 2, 'declaration_pending': 4}}},
            'artifacts': [{'id': 'f1', 'filename': 'Design fixture — article.pdf', 'role': 'main_pdf', 'status': 'verified', 'sha256': 'fixture-not-a-real-artifact'}]}],
        'events': [{'id': 1, 'type': 'progress', 'operator_message_id': 'u1', 'data': {'event_type': 'model_selected', 'summary': 'model selected', 'routing_reason': 'Validated repeated steps can run without a model.', 'mode': 'deterministic'}}, {'id': 2, 'type': 'status', 'operator_message_id': 'u2', 'data': {'status': 'paused', 'summary': 'The running work has acknowledged the pause.'}}], 'events_cursor': 2}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.server, wait_until='domcontentloaded')
        page.get_by_label('Operator token').wait_for(timeout=20000)
        if page.get_by_label('Operator token').count():
            page.get_by_label('Operator token').fill(token)
            page.get_by_role('button', name='Connect workspace').click()
        page.get_by_role('heading', name='What would you like to collect?').wait_for(timeout=20000)
        page.evaluate('document.fonts.ready')
        checks.append('real_operator_login')

        def capture(name):
            page.wait_for_timeout(150)
            path = args.output / (name + '.png')
            page.screenshot(path=str(path), full_page=True)
            screenshots.append(str(path))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), name
            checks.append(name + '_no_horizontal_overflow')

        for width in (390, 768, 1024, 1440):
            page.set_viewport_size({'width': width, 'height': 1000 if width > 768 else 844})
            page.get_by_label('Message ORE').fill('저널의 논문 원문과 supplementary material을 수집하고 진행 상태를 보여 주세요.')
            capture(f'new-light-{width}')
            button = page.get_by_role('button', name='Send message')
            bounds = button.bounding_box()
            assert bounds and bounds['width'] >= 43 and bounds['height'] >= 43
        page.get_by_label('Message ORE').fill('')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.get_by_role('button', name='Switch to dark theme').click()
        capture('new-dark-1440')
        assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
        page.reload(wait_until='domcontentloaded')
        page.get_by_label('Message ORE').wait_for()
        assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
        checks.append('dark_preference_survives_reload')
        page.get_by_role('button', name='Switch to light theme').click()

        def fixture(route):
            url = urlsplit(route.request.url)
            if url.path == '/v1/conversations/design-fixture':
                route.fulfill(json=conversation)
            elif url.path == '/v1/conversations/design-fixture/events':
                route.fulfill(status=200, content_type='text/event-stream', body=': fixture keepalive\n\n')
            else:
                route.continue_()
        page.route('**/v1/conversations/design-fixture**', fixture)
        page.goto(args.server + '/chat/design-fixture', wait_until='domcontentloaded')
        page.get_by_role('heading', name='Design fixture · June archive').wait_for()
        page.locator('.chat-transcript').evaluate('(element)=>element.scrollTop=0')
        capture('timeline-light-1440')
        page.get_by_role('button', name='Open collected results').click()
        capture('results-light-1440')
        page.get_by_role('button', name='Close results', exact=True).click()
        page.get_by_role('button', name='Switch to dark theme').click()
        page.locator('.chat-transcript').evaluate('(element)=>element.scrollTop=element.scrollHeight')
        capture('timeline-dark-1440')
        for width in (390, 768, 1024):
            page.set_viewport_size({'width': width, 'height': 844})
            capture(f'timeline-dark-{width}')
            page.get_by_role('button', name='Open collected results').click()
            page.get_by_role('button', name='Close results', exact=True).wait_for()
            capture(f'results-dark-{width}')
            page.keyboard.press('Escape')
            assert page.get_by_role('button', name='Open collected results').evaluate('(node)=>document.activeElement===node')
            page.get_by_role('button', name='Toggle navigation').click()
            page.get_by_role('button', name='New chat', exact=True).wait_for(state='visible')
            page.keyboard.press('Escape')
            assert page.get_by_role('button', name='Toggle navigation').evaluate('(node)=>document.activeElement===node')
        checks.append('mobile_drawer_escape_and_focus_restore')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        for route in ('jobs', 'connections', 'runes', 'handoffs', 'browser'):
            page.goto(args.server + '/' + route, wait_until='domcontentloaded')
            page.wait_for_timeout(600)
            capture('legacy-dark-' + route)
        assert not errors, errors
        checks.append('legacy_routes_render_without_browser_errors')
        checks.append('scope_unknown_has_no_collection_percentage')
        browser.close()
    result = {'ok': not errors, 'validated_at': datetime.now(timezone.utc).isoformat(), 'kind': 'real_auth_and_operational_routes_plus_labelled_read_only_design_fixture', 'external_collection_verified': False, 'real_llm_streaming_verified': False, 'checks': checks, 'screenshots': screenshots, 'page_errors': errors, 'logo_sha256': hashlib.sha256(Path('ORE_Original.svg').read_bytes()).hexdigest()}
    (args.output / 'report.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'ok': result['ok'], 'checks': len(checks), 'screenshots': len(screenshots)}))


if __name__ == '__main__':
    run()
