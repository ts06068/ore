#!/usr/bin/env python3
"""Actual Chrome/X11 acceptance against fixtures inside its own container.

Inputs are scripted coordinates, never LLM decisions. This checks neither a
publisher nor Cloudflare. No coordinator service, operator profile, credentials,
or pre-existing job is used. A new state directory is retained for inspection.

The Docker daemon must see --state-root at --host-state-root when the caller is
itself in a container. Both paths denote the same directory on their respective
filesystems. Resource enforcement defaults to the runtime's cgroup mode; select
--resource-mode watchdog explicitly only on a host that needs that mode.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import struct
import time

from ore.config import Settings
from ore.engine import Engine
from ore.tools import ToolRuntime


CSV = b"participant_id,measurement\nfixture-001,137\nfixture-002,90\n"
PRIMARY_PORT, FRAME_PORT = 18761, 18762

# This server runs ONLY inside the session container. Both listening sockets are
# bound to its loopback interface, with no Docker-published port or host server.
# Cookie values are fixed, synthetic fixture data; the log records booleans only.
FIXTURE_SERVER = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import json, threading, time, urllib.request, urllib.error

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / 'config.json').read_text())
CSV = __CSV_REPR__
PORT_A, PORT_B = CONFIG['ports']
ORIGIN_A, ORIGIN_B = 'http://127.0.0.1:'+str(PORT_A), 'http://127.0.0.1:'+str(PORT_B)
LOCK = threading.Lock()

def log(value):
    with LOCK:
        with (ROOT / 'requests.jsonl').open('a') as out:
            out.write(json.dumps({'time': time.time(), **value})+'\n')

def html(body, title='ORE native fixture'):
    return ('<!doctype html><html><head><meta charset="utf-8"><title>'+title+
            '</title><style>body{margin:0;font:30px Arial,sans-serif;color:#111;background:white}'
            'h1,p{margin:20px}iframe{display:block;margin:20px;width:1100px;height:270px}'
            '</style></head><body>'+body+'</body></html>').encode()

class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        authenticated = 'ore_fixture=granted' in self.headers.get('Cookie', '').split('; ')
        log({'port': self.server.server_port, 'path': path, 'cookie_present': authenticated,
             'event': parse_qs(parsed.query).get('name', [None])[0],
             'input_value': parse_qs(parsed.query).get('value', [None])[0] if path == '/input-event' else None})
        headers = {}
        content_type = 'text/html; charset=utf-8'
        status = 200
        if path == '/frames':
            body = html('''<h1>ORE FRAME POLICY FIXTURE</h1>
              <p id="image-state">IMAGE WAITING</p><p id="frame-state">FRAME WAITING</p>
              <img alt="fixture image" width="30" height="30" src="'''+ORIGIN_B+'''/pixel.svg"
                   onload="document.getElementById('image-state').textContent='IMAGE CONFIRMED';fetch('/client-event?name=image_loaded')">
              <iframe id="fixture-frame" src="'''+ORIGIN_B+'''/frame" title="support origin frame"></iframe>
              <script>window.addEventListener('message', function(e){
                if(e.origin === '''+json.dumps(ORIGIN_B)+''' && e.source === document.getElementById('fixture-frame').contentWindow
                   && e.data === 'ORE_FRAME_MESSAGE'){
                  document.getElementById('frame-state').textContent='FRAME CONFIRMED';
                  fetch('/client-event?name=frame_message');
                }});</script>''')
        elif path == '/frame':
            body = html('<h1>FRAME CONTENT DELTA</h1><script>parent.postMessage("ORE_FRAME_MESSAGE",'+json.dumps(ORIGIN_A)+');</script>')
        elif path == '/pixel.svg':
            content_type = 'image/svg+xml'
            body = b'<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30"><rect width="30" height="30" fill="green"/></svg>'
        elif path == '/scroll':
            body = html('<section style="height:800px;background:#eef"><h1>SCROLL TOP ALPHA</h1>'
                        '<p>Use the actual desktop wheel to reveal the lower marker.</p></section>'
                        '<section style="height:1600px;background:#efe">'+
                        ''.join('<p style="height:140px;margin:0;padding:20px">SCROLL BOTTOM OMEGA</p>' for _ in range(8))+'</section>')
        elif path == '/input':
            body = html('''<h1>ORE INPUT FOCUS FIXTURE</h1>
                <input id="fixture-input" aria-label="Fixture input" style="position:absolute;left:20px;top:130px;width:800px;height:80px;font:36px Arial"
                       oninput="document.getElementById('echo').textContent='INPUT VALUE '+this.value;fetch('/input-event?value='+encodeURIComponent(this.value))">
                <p id="echo" style="position:absolute;top:300px">INPUT EMPTY</p>''')
        elif path == '/download':
            body = html('<a href="/grant" style="display:block;margin:20px;width:900px;height:300px;'
                        'padding:30px;background:#def;color:#124;text-decoration:none;box-sizing:border-box">'
                        '<strong>DOWNLOAD COOKIE FIXTURE</strong><p>Click this large native browser link.</p></a>')
        elif path == '/grant':
            status, body = 302, b''
            headers = {'Set-Cookie': 'ore_fixture=granted; Path=/; HttpOnly; SameSite=Lax', 'Location': '/private.csv'}
        elif path == '/private.csv':
            if authenticated:
                body, content_type = CSV, 'text/csv'
                headers['Content-Disposition'] = 'attachment; filename="ore-native-fixture.csv"'
            else:
                status, body = 401, b'Fixture cookie required'
                content_type = 'text/plain'
        elif path in ('/client-event', '/input-event'):
            body, content_type = b'ok', 'text/plain'
        else:
            status, body = 404, b'Not found'
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in headers.items(): self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass

servers = [ThreadingHTTPServer(('127.0.0.1', port), Page) for port in (PORT_A, PORT_B)]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with urllib.request.urlopen(ORIGIN_A+'/private.csv', timeout=5) as response:
        no_cookie_status = response.status
except urllib.error.HTTPError as exc:
    no_cookie_status = exc.code
(ROOT/'ready.json').write_text(json.dumps({'ports': [PORT_A, PORT_B], 'bind_host': '127.0.0.1',
                                         'no_cookie_status': no_cookie_status}))
threading.Event().wait()
""".replace('__CSV_REPR__', repr(CSV))


def utc():
    return datetime.now(timezone.utc).isoformat()


def require(case, name, condition, **evidence):
    check = {'name': name, 'passed': bool(condition), **evidence}
    case['checks'].append(check)
    if not condition:
        raise AssertionError(name)


def requests(directory):
    path = directory / 'requests.jsonl'
    values = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                # A concurrently appended final line can be retried next poll.
                continue
    return values


def capture(case, runtime, observation, name, directory):
    image = runtime.last_image
    if not isinstance(image, str) or not image.startswith('data:image/png;base64,'):
        raise AssertionError('Native observation did not return a PNG data URL')
    data = base64.b64decode(image.split(',', 1)[1], validate=True)
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) < 24:
        raise AssertionError('Invalid PNG observation')
    dimensions = struct.unpack('>II', data[16:24])
    digest = hashlib.sha256(data).hexdigest()
    require(case, name + '_screen', dimensions == (1280, 800)
            and digest == observation.get('screenshot_sha256'),
            dimensions=list(dimensions), sha256=digest,
            reported_sha256=observation.get('screenshot_sha256'))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + '.png')
    path.write_bytes(data)
    case['observations'].append({'name': name, 'path': str(path), 'sha256': digest,
        'url': observation.get('url'), 'observation_kind': observation.get('observation_kind'),
        'http_status': observation.get('http_status'), 'text': observation.get('text', '')})
    return digest


async def fixture_start(engine, session, case):
    desktop = engine.browser.desktop_runtime
    native = desktop.get(session.id)
    directory = native.state_dir / 'acceptance-fixture'
    directory.mkdir(mode=0o700)
    (directory / 'server.py').write_text(FIXTURE_SERVER)
    (directory / 'config.json').write_text(json.dumps({'ports': [PRIMARY_PORT, FRAME_PORT]}))
    # Fixed argv, no shell, no additional mounts, no published/listening host port.
    await desktop._command(['exec', '--detach', native.container_name,
                            'python3', '/state/acceptance-fixture/server.py'],
                           timeout=10, max_output=65536)
    deadline = time.monotonic() + 15
    while not (directory / 'ready.json').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Container-local fixture did not become ready')
        await asyncio.sleep(.1)
    ready = json.loads((directory / 'ready.json').read_text())
    case['fixture'] = {'directory': str(directory), 'container_name': native.container_name,
                       'fixture_sha256': hashlib.sha256(FIXTURE_SERVER.encode()).hexdigest(), **ready}
    require(case, 'cookie_required_without_browser_session', ready['no_cookie_status'] == 401)
    policy = json.loads((desktop.state_dir / 'policies' /
                         (hashlib.sha256(session.id.encode()).hexdigest() + '.json')).read_text())
    case['native_url_policy'] = {key: policy[key] for key in ('URLBlocklist', 'URLAllowlist')}
    return directory


async def run_case(engine, name, allow_frame):
    started = time.monotonic()
    case = {'name': name, 'started_at': utc(), 'checks': [], 'observations': [], 'passed': False}
    origin_a, origin_b = f'http://127.0.0.1:{PRIMARY_PORT}', f'http://127.0.0.1:{FRAME_PORT}'
    allowed = [origin_a, origin_b] if allow_frame else [origin_a]
    job = engine.create({'goal': 'Controlled native desktop fixture acceptance; scripted inputs only',
        'urls': [origin_a + '/frames'], 'allowed_origins': allowed,
        'scope': {'browser_support_origins': [origin_b]} if not allow_frame else {},
        'sources': ['general_web'], 'access_profile_ref': 'desktop-fixture',
        'artifact_roles': ['attachment'] if allow_frame else [], 'completeness': 'bounded',
        'budget': {'max_turns': 1, 'max_seconds': 240, 'max_agent_workers': 1, 'max_bytes': 1024 * 1024},
        'limits': {'origin_min_interval_seconds': 0, 'max_browser_actions': 40,
                   'max_artifact_bytes': 1024 * 1024},
        'acceptance_mode': 'scripted_native_desktop_fixture_no_model'})
    task = engine.store.claim_task('desktop-acceptance', lease_seconds=240, job_id=job['id'])
    if task is None:
        raise RuntimeError('Could not claim isolated acceptance task')
    runtime = ToolRuntime(engine, job['id'], task)
    case['job_id'], case['task_id'] = job['id'], task['id']
    session = None
    fixture_dir = None
    screenshots = engine.settings.state_dir / 'reports' / name
    try:
        async with asyncio.timeout(240):
            opened = await runtime.execute('browser_open', {})
            require(case, 'native_backend', opened.get('transport') == 'desktop_chrome'
                    and opened.get('http_status') is None,
                    transport=opened.get('transport'), http_status=opened.get('http_status'))
            session = engine.browser.get(opened['session_id'])
            fixture_dir = await fixture_start(engine, session, case)

            async def action(kind, **arguments):
                result = await runtime.execute('browser_action', {'session_id': session.id,
                    'epoch': session.epoch, 'action': kind, **arguments})
                if result.get('error') or result.get('needs_user'):
                    raise AssertionError('Unexpected fixture browser failure or handoff')
                return result

            frame = await action('navigate', url=origin_a + '/frames')
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                events = requests(fixture_dir)
                if any(row.get('event') == 'image_loaded' for row in events) and (
                    not allow_frame or any(row.get('event') == 'frame_message' for row in events)):
                    break
                await asyncio.sleep(.2)
            # Keep a bounded observation window for a denied iframe; the allowed
            # image on the same server demonstrates the fixture origin is alive.
            await asyncio.sleep(2)
            frame = await runtime.execute('browser_observe', {'session_id': session.id})
            capture(case, runtime, frame, 'frame_policy', screenshots)
            events = requests(fixture_dir)
            image_hit = any(row['port'] == FRAME_PORT and row['path'] == '/pixel.svg' for row in events)
            frame_hit = any(row['port'] == FRAME_PORT and row['path'] == '/frame' for row in events)
            message = any(row.get('event') == 'frame_message' for row in events)
            require(case, 'observed_url', frame.get('url') == origin_a + '/frames', url=frame.get('url'))
            require(case, 'cross_origin_image_control', image_hit and 'IMAGE CONFIRMED' in frame.get('text', ''),
                    image_requested=image_hit)
            require(case, 'iframe_policy', frame_hit == allow_frame and message == allow_frame,
                    explicitly_allowed=allow_frame, frame_document_requested=frame_hit,
                    parent_received_expected_message=message,
                    observation_window_seconds=2)
            if allow_frame:
                require(case, 'allowed_frame_visible', 'FRAME CONTENT DELTA' in frame.get('text', ''),
                        marker='FRAME CONTENT DELTA')

                top = await action('navigate', url=origin_a + '/scroll')
                top_sha = capture(case, runtime, top, 'scroll_before', screenshots)
                require(case, 'scroll_top_marker', 'SCROLL TOP ALPHA' in top.get('text', ''))
                # Known coordinates address the controlled layout in the whole
                # 1280x800 desktop, including Chrome's toolbar. No DOM is queried.
                await action('click', x=300, y=300)
                bottom = await action('scroll', deltaY=1200)
                bottom_sha = capture(case, runtime, bottom, 'scroll_after', screenshots)
                require(case, 'native_scroll_changes_visible_marker',
                        top_sha != bottom_sha and 'SCROLL BOTTOM OMEGA' in bottom.get('text', '')
                        and 'SCROLL TOP ALPHA' not in bottom.get('text', ''),
                        before_sha256=top_sha, after_sha256=bottom_sha)

                input_page = await action('navigate', url=origin_a + '/input')
                capture(case, runtime, input_page, 'input_before_click', screenshots)
                await action('click', x=300, y=250)
                focused = await runtime.execute('browser_observe', {'session_id': session.id})
                capture(case, runtime, focused, 'input_after_explicit_observe', screenshots)
                typed = await action('type', text='OREFOCUS137')
                capture(case, runtime, typed, 'input_after_type', screenshots)
                input_events = requests(fixture_dir)
                require(case, 'input_focus_survives_url_observation',
                        typed.get('url') == origin_a + '/input'
                        and any(row.get('input_value') == 'OREFOCUS137' for row in input_events),
                        observed_url=typed.get('url'), expected_input_value='OREFOCUS137',
                        observed_values=[row['input_value'] for row in input_events if row.get('input_value') is not None])

                download_page = await action('navigate', url=origin_a + '/download')
                capture(case, runtime, download_page, 'before_download_click', screenshots)
                require(case, 'download_control_visible', 'DOWNLOAD COOKIE FIXTURE' in download_page.get('text', ''))
                received = await action('click', x=300, y=250)
                deadline = time.monotonic() + 25
                while not received.get('downloads') and time.monotonic() < deadline:
                    await asyncio.sleep(.25)
                    received = await runtime.execute('browser_observe', {'session_id': session.id})
                capture(case, runtime, received, 'after_download_click', screenshots)
                downloads = received.get('downloads', [])
                require(case, 'native_completed_receipt', len(downloads) == 1
                        and downloads[0].get('receipt_kind') == 'chrome_download_history',
                        count=len(downloads), receipts=[{k:row.get(k) for k in
                            ('receipt_kind', 'native_download_id', 'bytes', 'sha256', 'requested_source_url', 'url', 'redirect_chain')}
                            for row in downloads])
                events = requests(fixture_dir)
                require(case, 'native_click_granted_cookie_then_authenticated_download',
                        any(row['path'] == '/grant' for row in events)
                        and any(row['path'] == '/private.csv' and row['cookie_present'] for row in events))
                receipt = downloads[0]
                require(case, 'receipt_redirect_chain', receipt.get('requested_source_url') == origin_a + '/grant'
                        and receipt.get('url') == origin_a + '/private.csv'
                        and receipt.get('redirect_chain') == [origin_a + '/grant', origin_a + '/private.csv'])
                resource = await runtime.execute('resource', {'id': 'controlled-native-csv',
                    'title': 'Controlled native desktop fixture', 'url': origin_a + '/download',
                    'classification': 'included', 'reason': 'Synthetic fixture; no journal collection claim'})
                artifact = await runtime.execute('artifact_commit', {'session_id': session.id,
                    'download_index': 0, 'resource_id': resource['id'], 'role': 'attachment'})
                content = Path(artifact['path']).read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                case['artifact'] = {key: artifact.get(key) for key in
                    ('id', 'path', 'bytes', 'sha256', 'status', 'integrity', 'media_type',
                     'source', 'source_url', 'requested_source_url', 'redirect_chain')}
                require(case, 'ore_committed_exact_csv', content == CSV and artifact['status'] == 'verified'
                        and artifact['sha256'] == digest == hashlib.sha256(CSV).hexdigest(),
                        bytes=len(content), expected_sha256=hashlib.sha256(CSV).hexdigest(), sha256=digest)
                current = engine.store.get_job(job['id'])
                scope = f"{job['id']}:{current['revision']}:{current['generation']}"
                budget = engine.store.get_budget(scope, 'download_bytes') or {}
                require(case, 'download_accounted_once', budget.get('used') == len(CSV), budget=budget)
            else:
                await runtime.execute('resource', {'id': 'controlled-frame-policy',
                    'title': 'Controlled native frame policy fixture', 'url': origin_a + '/frames',
                    'classification': 'included', 'reason': 'Synthetic policy observation; no article or artifact requirement'})

            finished = await runtime.execute('finish', {'summary': 'Scripted local desktop checks passed; no LLM, publisher, or Cloudflare claim'})
            case['audit'] = finished['audit']
            require(case, 'bounded_audit', finished['audit']['status'] == 'complete_within_scope')
            engine.store.finish_task(task['id'], task['worker_id'], task['fence'],
                {'acceptance_passed': True, 'audit': finished['audit']}, task['revision'])
            engine.reconcile(job['id'])
            case['passed'] = True
    except Exception as exc:
        case['error'] = {'type': type(exc).__name__, 'message': str(exc)[:1000]}
        if session is not None and not session.closed:
            try:
                failed_png = await session.page.screenshot()
                screenshots.mkdir(parents=True, exist_ok=True)
                failure_path = screenshots / 'failure-screen.png'
                failure_path.write_bytes(failed_png)
                case['failure_screenshot'] = str(failure_path)
            except Exception:
                pass
        engine.store.update_job(job['id'], status='needs_review')
    finally:
        if fixture_dir is not None:
            case['fixture_requests'] = requests(fixture_dir)
        if session is not None and not session.closed:
            try:
                await engine.browser.close_session(session.id)
            except Exception as exc:
                case['cleanup_error'] = {'type': type(exc).__name__, 'message': str(exc)[:300]}
                case['passed'] = False
        case['job_status'] = engine.store.get_job(job['id'])['status']
        case['finished_at'] = utc()
        case['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return case


async def run(args):
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4)
    state = (args.state_root / run_id).resolve()
    desktop_host = (args.host_state_root / run_id / 'desktop').resolve() if args.host_state_root else None
    settings = Settings(state_dir=state, database_url=f"sqlite:///{state / 'ore.db'}",
        auth_token=secrets.token_urlsafe(32), max_workers=0, execution_backend='local',
        browser_backend='desktop_chrome', browser_proxy=None, desktop_image=args.image,
        desktop_resource_mode=args.resource_mode, desktop_host_state_dir=desktop_host)
    engine = Engine(settings)
    engine.save_profile({'id': 'desktop-fixture', 'name': 'Isolated native desktop acceptance',
        'allow_private_network': True, 'persist_session': False, 'browser_backend': 'desktop_chrome',
        'retrieval_policy': {'mode': 'official_first', 'browser_fallback': True}})
    report = {'schema_version': 'ore.desktop-acceptance/v1', 'started_at': utc(), 'passed': False,
        'state_dir': str(state), 'image': args.image, 'resource_mode': args.resource_mode,
        'mode': 'scripted_os_inputs_real_native_chrome_local_fixture', 'model_calls': 0,
        'publisher_tested': False, 'cloudflare_tested': False, 'onboarding_credentials_used': False,
        'fixture_network': 'Two loopback ports inside each desktop container; no published host ports',
        'limitations': ['No LLM workflow is evaluated.', 'No journal, Cloudflare, or systematic completeness result.',
                        'Native URLAllowlist exceptions permit both iframe and top-level navigation.'],
        'cases': []}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        for name, allowed in [('support_origin_not_navigation_allowed', False), ('explicit_second_origin_allowed', True)]:
            report['cases'].append(await run_case(engine, name, allowed))
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'case': name, 'passed': report['cases'][-1]['passed'],
                              'error': report['cases'][-1].get('error')}), flush=True)
    except Exception as exc:
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)[:1000]}
    finally:
        await engine.stop()
    report['finished_at'] = utc()
    report['passed'] = len(report['cases']) == 2 and all(case['passed'] for case in report['cases'])
    report['artifact_count'] = sum('artifact' in case for case in report['cases'])
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'report': str(args.report.resolve()),
                      'state_dir': str(state), 'artifact_count': report['artifact_count']}), flush=True)
    return report['passed']


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, default=root / '.ore/acceptance/desktop')
    parser.add_argument('--host-state-root', type=Path,
                        help='Docker-host path corresponding exactly to --state-root')
    parser.add_argument('--report', type=Path, default=root / '.ore/reports/desktop-acceptance.json')
    parser.add_argument('--image', default='ore-desktop:0.2.0rc1')
    parser.add_argument('--resource-mode', choices=('cgroup', 'watchdog'), default='cgroup')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == '__main__':
    main()
