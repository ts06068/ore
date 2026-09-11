"""Bounded anonymous native-Chrome checks; never reset production challenge records."""
from __future__ import annotations
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

from ore.config import Settings
from ore.engine import Engine
from ore.policy import redact
from ore.tools import ToolRuntime
from ore.desktop_browser import issue_checkpoint_identity

TARGETS = {
    'jacc': ('https://www.jacc.org/toc/jacc/83/1', ['Vol. 83 No. 1', 'January 2, 2024']),
    'circulation': ('https://www.ahajournals.org/toc/circ/149/1', ['Circulation']),
    'ehj': ('https://academic.oup.com/eurheartj/issue/45/1', ['European Heart Journal']),
    'jama-cardiology': ('https://jamanetwork.com/journals/jamacardiology/issue/9/1', ['JAMA Cardiology']),
    'cloudflare-debug': ('https://debug.challenges.cloudflare.com/', ['Browser']),
}

async def check(args):
    from urllib.parse import urlsplit
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    state = (args.state_root / stamp).resolve()
    host_desktop = args.host_state_root / stamp / 'desktop' if args.host_state_root else None
    engine = Engine(Settings(state_dir=state, max_workers=0, browser_backend='desktop_chrome', desktop_resource_mode=args.resource_mode, desktop_host_state_dir=host_desktop))
    output = {'observed_at': datetime.now(timezone.utc).isoformat(), 'state_dir': str(state),
        'transport': 'desktop_chrome', 'playwright_used': False, 'credentials_used': False,
        'production_challenge_records_changed': False, 'real_llm': args.agent, 'resource_mode': args.resource_mode, 'results': []}
    try:
        for journal in args.journal or ['jacc', 'circulation', 'ehj', 'jama-cardiology']:
            url, markers = TARGETS[journal]
            origin = 'https://' + urlsplit(url).netloc
            profile_id = 'anonymous-desktop-' + journal
            engine.save_profile({'id': profile_id, 'browser_backend': 'desktop_chrome',
                'persist_session': False, 'desktop_success_text': [] if issue_checkpoint_identity(url) else markers,
                'retrieval_policy': {'mode': 'official_first', 'browser_fallback': True}})
            mission = {'goal': f'Inspect the exact requested page {url} in desktop Chrome using screenshots and OS browser actions. '
                'If a visible verification checkbox appears, reserve one challenge attempt and click its observed coordinates. '
                'After the result, inspect the target. Do not fabricate URLs, cookies or successful access. '
                'This is a bounded browser compatibility test, not a complete collection. Do not search, fetch, delegate, '
                'register documents or download files. If the target page loads, finish with the exact observed issue title. '
                'If verification fails after the one allowed attempt, hand off. No repeated reloads or budget resets.',
                'urls': [url], 'allowed_origins': [origin, 'https://challenges.cloudflare.com'], 'artifact_roles': [], 'sources': [],
                'access_profile_ref': profile_id, 'routing': {'mode': 'fixed', 'model': 'gpt-6-astra', 'effort': 'high'},
                'retrieval_policy': {'mode': 'official_first', 'browser_fallback': True},
                'budget': {'max_turns': 12, 'max_seconds': args.timeout, 'max_agent_workers': 1},
                'on_challenge': {'max_attempts_per_episode': 1, 'max_active_seconds': 60, 'recovery_settle_seconds': 30}}
            if issue_checkpoint_identity(url):
                mission['desktop_issue_checkpoint'] = url
            job = engine.create(mission)
            result = {'journal': journal, 'url': url, 'job_id': job['id'], 'target_loaded': False,
                'main_pdf_downloaded': False, 'supplements_downloaded': False}
            started = time.monotonic()
            print(json.dumps({'event': 'started', 'journal': journal, 'agent': args.agent, 'state': str(state)}), flush=True)
            try:
                if args.agent:
                    task = engine.store.claim_task('desktop-diagnostic', job_id=job['id'])
                    engine.store.update_job(job['id'], status='running')
                    await asyncio.wait_for(engine.execute_task(task, 'desktop-diagnostic'), args.timeout + 30)
                    sessions = [s for s in engine.browser.sessions.values() if s.job_id == job['id'] and not s.closed]
                    if not sessions:
                        raise RuntimeError('No desktop session was created by the agent')
                    session = sessions[0]
                else:
                    session = await engine.browser.create(job['id'], job['mission'], engine.profile(job['mission']))
                    await engine.browser.action(session.id, 'navigate', {'url': url})
                    await asyncio.sleep(8)
                # A late publisher response may arrive after the actor yielded. Observe
                # within a finite grace period; never click, refresh, or reset an episode.
                if args.agent:
                    deadline = asyncio.get_running_loop().time() + 30
                    while asyncio.get_running_loop().time() < deadline:
                        from ore.desktop_browser import target_recovered
                        recovered, _ = await target_recovered(session)
                        if recovered:
                            await engine.browser.verify_challenge(session.id)
                            break
                        await asyncio.sleep(2)
                observation = await engine.browser.observe(session.id, owner=session.control)
                image = base64.b64decode(observation.pop('image_url').split(',', 1)[1])
                screenshot = state / 'reports' / (journal + '.png'); screenshot.write_bytes(image)
                from ore.desktop_browser import target_recovered
                recovered, evidence = await target_recovered(session)
                exact_target = urlsplit(session.page.url)._replace(query='', fragment='') == urlsplit(url)._replace(query='', fragment='')
                result.update(target_loaded=recovered and exact_target, exact_target_url_observed=exact_target, recovery_evidence=evidence, screenshot=str(screenshot),
                    screenshot_sha256=hashlib.sha256(image).hexdigest(), observation=redact(observation))
                episodes = [engine.store.get_challenge(s.challenge_id) for s in sessions if s.challenge_id] if args.agent else []
                result['challenge_episodes'] = [{k: r.get(k) for k in ('attempts', 'max_attempts', 'state', 'active_seconds')} for r in episodes if r]
                events = engine.store.events(job['id'])
                result['agent_decisions'] = [redact(e['payload']['decision']) for e in events if e['type'] == 'agent_decision']
            except Exception as exc:
                result['error'] = {'type': type(exc).__name__, 'reason': str(exc)[:1500]}
            finally:
                for session in list(engine.browser.sessions.values()):
                    if session.job_id == job['id'] and not session.closed:
                        await engine.browser.close_session(session.id)
            result['elapsed_seconds'] = round(time.monotonic() - started, 2)
            output['results'].append(result)
            (state / 'reports' / 'desktop-diagnosis.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'event': 'finished', 'journal': journal, 'target_loaded': result['target_loaded'],
                'error': result.get('error'), 'elapsed_seconds': result['elapsed_seconds']}), flush=True)
    finally:
        await engine.stop()
    print(json.dumps({'report': str(state / 'reports' / 'desktop-diagnosis.json')}), flush=True)
    return 0 if all(r['target_loaded'] for r in output['results']) else 2

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', choices=list(TARGETS), action='append')
    parser.add_argument('--resource-mode', choices=['cgroup', 'watchdog'], default='cgroup')
    parser.add_argument('--agent', action='store_true', help='Use the existing authenticated Codex backend with ORE tools')
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--host-state-root', type=Path, help='Docker-host path corresponding to --state-root')
    parser.add_argument('--state-root', type=Path, default=Path('.ore/desktop-validation'))
    args = parser.parse_args()
    if not 30 <= args.timeout <= 600: parser.error('timeout must be between 30 and 600 seconds')
    raise SystemExit(asyncio.run(check(args)))
