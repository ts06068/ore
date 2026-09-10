"""Exercise the built console against a real ORE server without any model turn.

Run inside the ORE image with ORE_UI_SMOKE_TOKEN set to a disposable test token.
The server must have zero local/remote agent workers and disposable state.
"""
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get('ORE_UI_SMOKE_URL', 'http://127.0.0.1:8765')
TOKEN = os.environ['ORE_UI_SMOKE_TOKEN']
OUTPUT = Path(os.environ.get('ORE_UI_SMOKE_OUTPUT', '/tmp/ore-ui-smoke'))
OUTPUT.mkdir(parents=True, exist_ok=True)
state = {'clicks': 0}

class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == '/clicked':
            state['clicks'] += 1
            body = b'ok'
        else:
            body = b'''<!doctype html><html><head><title>ORE remote fixture</title></head>
            <body style="margin:0;background:#f0f5e4;font-family:sans-serif"><button
            style="position:absolute;left:40px;top:40px;width:140px;height:50px"
            onclick="fetch('/clicked');this.textContent='Clicked'">Remote input test</button>
            <h2 style="position:absolute;left:40px;top:120px">Browser takeover fixture</h2></body></html>'''
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

fixture = ThreadingHTTPServer(('127.0.0.1', 18999), Fixture)
threading.Thread(target=fixture.serve_forever, daemon=True).start()

def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data, headers={
        'Authorization': 'Bearer '+TOKEN, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as result:
        return json.load(result)

request('/v1/access-profiles', {'id':'ui-fixture', 'name':'UI fixture',
    'network':'public', 'allowed_hosts':['127.0.0.1'],
    'origins':['http://127.0.0.1:18999'], 'sources':{}})
results = []
errors = []
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440, 'height':1050}, device_scale_factor=1)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(BASE, wait_until='networkidle')
        page.get_by_label('Operator token').wait_for()
        page.screenshot(path=str(OUTPUT/'login.png'), full_page=True)
        page.get_by_label('Operator token').fill(TOKEN)
        page.get_by_role('button', name='Connect workspace').click()
        page.get_by_role('heading', name='Missions', exact=True).wait_for()
        results.append('operator_login')
        assert TOKEN not in page.evaluate('JSON.stringify(localStorage)')
        page.reload(wait_until='networkidle')
        page.get_by_role('heading', name='Missions', exact=True).wait_for()
        results.append('cookie_session_restore')
        page.get_by_role('button', name='New mission', exact=False).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Mission name').fill('UI integration smoke')
        dialog.get_by_role('textbox', name=re.compile('^Instructions')).fill('Collect original research and all supplements; record unavailable artifacts.')
        dialog.get_by_role('textbox', name=re.compile('^Search query')).fill('transplantation')
        dialog.get_by_label('Run after creating').uncheck()
        dialog.get_by_role('button', name='Create mission', exact=True).click()
        page.get_by_role('heading', name='UI integration smoke', exact=True).wait_for()
        page.get_by_role('button', name='Coverage', exact=True).click()
        page.get_by_text('Coverage report is not available yet').wait_for(state='hidden')
        created = request('/v1/jobs')
        assert len(created) == 1, created
        job_id = created[0]['id']
        assert created[0]['status'] == 'draft', created[0]['status']
        results.append('mission_create_as_draft_and_audit_display')
        page.get_by_role('button', name='Run mission', exact=True).click()
        page.get_by_role('button', name='Pause', exact=True).wait_for()
        assert request('/v1/jobs/'+job_id)['status'] == 'running'
        page.get_by_role('button', name='Pause', exact=True).click()
        page.get_by_role('button', name='Resume', exact=True).wait_for()
        assert request('/v1/jobs/'+job_id)['status'] == 'paused'
        results.append('explicit_run_and_pause_without_model_workers')
        page.screenshot(path=str(OUTPUT/'mission.png'), full_page=True)
        page.get_by_role('button', name='Rune library', exact=True).click()
        page.get_by_role('button', name='New Rune', exact=True).click()
        page.get_by_label('Protocol ID').fill('ui-smoke-protocol')
        page.get_by_role('button', name='Save version', exact=True).click()
        page.get_by_text('Rune saved', exact=True).wait_for()
        results.append('rune_validate_save')
        session = request('/v1/browser/sessions', {'url':'http://127.0.0.1:18999', 'access_profile_ref':'ui-fixture'})
        page.get_by_role('button', name=re.compile('^Browser')).click()
        canvas = page.get_by_label('Remote browser page')
        canvas.wait_for(timeout=30000)
        page.get_by_role('button', name='Return to agent', exact=True).wait_for(timeout=30000)
        box = canvas.bounding_box()
        width = canvas.evaluate('(node)=>node.width')
        height = canvas.evaluate('(node)=>node.height')
        page.mouse.click(box['x']+110/width*box['width'], box['y']+65/height*box['height'])
        deadline = time.monotonic()+10
        while not state['clicks'] and time.monotonic()<deadline:
            page.wait_for_timeout(100)
        assert state['clicks'] == 1, state
        results.append('authenticated_websocket_frame_and_scaled_input')
        page.screenshot(path=str(OUTPUT/'browser.png'), full_page=True)
        page.set_viewport_size({'width':390, 'height':844})
        page.get_by_role('button', name='Toggle navigation').click()
        page.get_by_role('button', name='Missions', exact=False).first.click()
        page.get_by_role('heading', name='Missions', exact=True).wait_for()
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUTPUT/'mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile layout overflows'
        results.append('mobile_layout')
        assert not errors, errors
        browser.close()
finally:
    fixture.shutdown()
summary = {'passed':results, 'page_errors':errors, 'model_turns':0}
(OUTPUT/'summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary))
