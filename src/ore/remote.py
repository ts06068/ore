"""Host-side Codex worker: tokens remain in the host's Codex installation."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid

import httpx

from .codex import BackendError, CodexBackend
from .engine import INSTRUCTIONS
from .providers import DECISION_SCHEMA

HEARTBEAT_SECONDS = 5


async def _run_while_owned(operation, pulse, interrupt):
    """Race the whole attempt with lease loss, including a blocked model turn."""
    pending = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait({pending, pulse}, return_when=asyncio.FIRST_COMPLETED)
        if pulse in done:
            pulse.result()
            raise RuntimeError('Worker heartbeat stopped unexpectedly')
        return pending.result()
    finally:
        if not pending.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(interrupt(), timeout=10)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def worker(server, token, *, parallel=1, codex_bin=None, once=False, job_ids=None):
    if parallel < 1:
        raise ValueError('parallel must be positive')
    async with httpx.AsyncClient(base_url=server.rstrip('/'),
            headers={'Authorization': f'Bearer {token}'}, timeout=180) as client:
        async with CodexBackend(codex_bin) as backend:
            models = await backend.models()

            async def post(path, data):
                response = await client.post(path, json=data)
                response.raise_for_status()
                return response.json()

            async def loop(index):
                ident = 'host-' + uuid.uuid4().hex[:12]
                await post('/v1/workers/register', {'id': ident, 'models': models})
                while True:
                    offer = await post(f'/v1/workers/{ident}/claim', {} if job_ids is None else {'job_ids': list(job_ids)})
                    if not offer['task']:
                        if once:
                            return
                        await asyncio.sleep(2)
                        continue
                    task = offer['task']
                    prefix = f"/v1/workers/{ident}/tasks/{task['id']}"
                    fence = {'fence': task['fence']}
                    tid = None

                    async def interrupt():
                        if tid:
                            await backend.interrupt(tid)

                    async def heartbeat():
                        while True:
                            await asyncio.sleep(HEARTBEAT_SECONDS)
                            await post(prefix + '/heartbeat', fence)

                    async def attempt():
                        nonlocal tid
                        prompt = json.dumps(offer, ensure_ascii=False, default=str)
                        image, failures, started = None, 0, time.monotonic()
                        budget = offer['job']['mission'].get('budget', {})
                        for step in range(budget.get('max_turns', 100)):
                            remaining = budget.get('max_seconds', 3600) - (time.monotonic() - started)
                            if remaining <= 0:
                                raise TimeoutError('Task time budget exhausted')
                            route = await post(prefix + '/route', {**fence, 'failures': failures})
                            if not tid:
                                tid = await backend.thread([], None, model=route['model'], instructions=INSTRUCTIONS)
                            result = await backend.run(tid, prompt, model=route['model'], effort=route['effort'],
                                timeout=min(600, remaining), output_schema=DECISION_SCHEMA,
                                images=[image] if image else None)
                            if result.get('turn', {}).get('status') == 'failed':
                                raise BackendError('Codex turn failed')
                            decision = json.loads(result['text'])
                            args = json.loads(decision['arguments'])
                            await post(prefix + '/decision', {**fence, 'thread_id': tid, 'step': step,
                                'decision': decision, 'usage': result.get('usage', {})})
                            try:
                                observed = await post(prefix + '/tool', {**fence, 'tool': decision['tool'], 'arguments': args})
                                image = observed.get('image_url')
                                prompt = json.dumps(observed['result'], ensure_ascii=False, default=str)
                                failures = 0
                            except httpx.HTTPStatusError as exc:
                                if exc.response.status_code == 409:
                                    raise
                                failures += 1
                                prompt = json.dumps({'tool_error': exc.response.text[:1500]})
                                image = None
                                if failures >= 3:
                                    raise
                            else:
                                if observed['result'].get('needs_user'):
                                    await post(prefix + '/fail', {**fence, 'state': 'awaiting_user',
                                        'error': {'code': 'needs_user', 'handoff_id': observed['result'].get('handoff_id')}})
                                    return
                                if decision['tool'] == 'finish':
                                    await post(prefix + '/finish', {**fence, 'result': observed['result']})
                                    return
                        raise TimeoutError('Task turn limit exhausted')

                    pulse = asyncio.create_task(heartbeat())
                    try:
                        await _run_while_owned(attempt(), pulse, interrupt)
                    except asyncio.CancelledError:
                        with contextlib.suppress(Exception):
                            await post(prefix + '/fail', {**fence, 'error': {'code': 'worker_cancelled'}})
                        raise
                    except Exception as exc:
                        with contextlib.suppress(Exception):
                            await post(prefix + '/fail', {**fence, 'error': {'code': type(exc).__name__}})
                    finally:
                        pulse.cancel()
                        await asyncio.gather(pulse, return_exceptions=True)
                    if once:
                        return

            await asyncio.gather(*(loop(i) for i in range(parallel)))
