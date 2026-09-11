"""Coordinator deadline enforcement while an actor is awaiting model output."""
from __future__ import annotations
import asyncio
import contextlib
from datetime import datetime, timezone


class ChallengeCoordinator:
    def __init__(self, engine):
        self.engine = engine
        self.task = None
        self.pending = {}

    def start(self):
        if self.task is None or self.task.done(): self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task: self.task.cancel()
        for task in self.pending.values(): task.cancel()
        await asyncio.gather(*([self.task] if self.task else []), *self.pending.values(), return_exceptions=True)
        self.pending.clear();self.task=None

    async def _loop(self):
        while True:
            await asyncio.sleep(.5)
            try: await self.tick()
            except asyncio.CancelledError:raise
            except Exception:
                # A transient transport failure leaves the pending handoff visible;
                # every browser input also checks the same persisted deadline.
                continue

    async def tick(self):
        now=datetime.now(timezone.utc)
        for sid, task in list(self.pending.items()):
            if task.done():
                with contextlib.suppress(Exception): task.result()
                self.pending.pop(sid, None)
        expired=set()
        for episode in self.engine.store.list_documents('challenge',all_generations=True):
            if episode.get('clock')!='elapsed' or episode['state']=='resolved':continue
            if datetime.fromisoformat(episode['deadline_at'])<=now or episode['state']=='awaiting_user':
                self.engine.store.adapt_challenge(episode['id'])
                expired.add(episode['id'])
        if not expired:return
        browser=self.engine.browser
        for sid, session in list(browser.sessions.items()):
            if session.closed or session.control!='agent' or session.challenge_id not in expired or sid in self.pending:continue
            self.pending[sid]=asyncio.create_task(browser.takeover(sid))
        execution=getattr(self.engine,'execution',None)
        if execution and execution.enabled:
            for session in await execution.list_sessions():
                sid=session.get('session_id') or session.get('id')
                if session.get('closed') or session.get('control')!='agent' or session.get('challenge_id') not in expired or sid in self.pending:continue
                job_id=session['job_id']
                assignment=execution._get('assignment',session.get('assignment_id')) or {}
                task_id=(assignment.get('task') or {}).get('id') or assignment.get('recovery_task_id')
                self.engine.handoffs.create(job_id,'challenge','Automatic access verification reached its limit.',
                    session_id=sid, task_id=task_id, context={'challenge_id':session['challenge_id'],'deadline_expired':True})
                self.pending[sid]=asyncio.create_task(execution.browser_command(sid,'takeover'))
