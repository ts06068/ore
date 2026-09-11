"""Durable admission targets. Feedback changes soft limits, never approved missions."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from .store import documents, jobs, tasks, utcnow, _utc


def approved_limits(mission):
    ceiling = int((mission.get('budget') or mission.get('limits') or {}).get('max_agent_workers', 5))
    policy = mission.get('parallelism') or {}
    return max(1, ceiling), min(max(1, int(policy.get('initial', 5))), max(1, ceiling))


def task_admission(task, mission):
    """Static admission hints are scheduler-owned, not model assertions."""
    hint = task.get('input', {}).get('_admission', {})
    needs_executor = hint.get('executor', task.get('kind') != 'workflow')
    raw_url = hint.get('url') or task.get('input', {}).get('url')
    if not raw_url and len(mission.get('urls', [])) == 1:
        raw_url = mission['urls'][0]
    parsed = urlsplit(raw_url or '')
    origin = f'{parsed.scheme}://{parsed.netloc.lower()}' if parsed.scheme in ('http', 'https') and parsed.netloc else None
    return {'executor': bool(needs_executor), 'browser': hint.get('browser', task.get('kind') != 'workflow'), 'origin': origin}


class AdaptiveScheduler:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.loop = None
        self.closed = False

    async def start(self):
        self.closed = False
        await self.tick()
        if not self.loop or self.loop.done():
            self.loop = asyncio.create_task(self._run())

    async def close(self):
        self.closed = True
        if self.loop:
            self.loop.cancel()
            await asyncio.gather(self.loop, return_exceptions=True)
            self.loop = None

    async def _run(self):
        while not self.closed:
            await asyncio.sleep(max(1, self.engine.settings.scheduler_interval_seconds))
            try: await self.tick()
            except asyncio.CancelledError: raise
            except Exception:
                # A failed observation never widens a previous admission limit.
                continue

    def snapshot(self):
        return {'global': self.store.get_document('scheduler.control', 'global'),
                'jobs': self.store.list_documents('scheduler.job'),
                'pool': self.store.get_document('pool.state', 'fleet')}

    async def tick(self, *, now=None, observation=None):
        now = float(time.time() if now is None else now)
        settings = self.engine.settings
        fleet = self.store.get_document('pool.state', 'fleet') or {}
        report = observation if observation is not None else fleet.get('resources', {})
        resource_pressure = any(float(report.get(key, 0)) >= .85 for key in ('cpu_ratio', 'memory_ratio'))
        execution = self.engine.execution
        workers = execution._list('worker') if execution.enabled else []
        live = [w for w in workers if w.get('state') in ('idle', 'busy') and now - w.get('last_seen', 0) < 30]
        active_task_ids = {t['id'] for job in self.store.list_jobs() for t in self.store.tasks(job['id']) if t['state'] == 'running'}
        pinned = {a['worker_id'] for a in execution._list('assignment')
                  if a['status'] == 'active' and ((a.get('task') or {}).get('id') not in active_task_ids)} if execution.enabled else set()
        ready_capacity = sum(w['id'] not in pinned for w in live)
        global_cap = max(0, int(settings.scheduler_global_limit))
        demand = 0
        with self.store._tx() as conn:
            current_jobs = conn.execute(select(jobs).where(jobs.c.state.in_(['queued', 'running', 'resuming']))).mappings().all()
            for job in current_jobs:
                mission = job['mission']; ceiling, initial = approved_limits(mission)
                old = self.store._doc(conn, 'scheduler.job', job['id'])
                row = dict(old['data']) if old and old['data'].get('job_revision') == job['revision'] else {
                    'id': job['id'], 'target': initial, 'stable_windows': 0, 'completed_seen': 0, 'failures_seen': 0,
                    'control_epoch': 0, 'last_observed_at': None, 'rate_seen': None}
                task_rows = conn.execute(select(tasks).where(tasks.c.job_id == job['id'], tasks.c.revision == job['revision'], tasks.c.generation == job['generation'])).mappings().all()
                running = [t for t in task_rows if t['state'] == 'running' and _utc(t['lease_expires_at']) > datetime.fromtimestamp(now, timezone.utc)]
                queued = [t for t in task_rows if t['state'] == 'queued' or (t['state'] == 'retry_wait' and (not t['retry_at'] or _utc(t['retry_at']).timestamp() <= now))]
                completed = sum(t['state'] == 'succeeded' for t in task_rows)
                failures = sum(bool(t.get('error')) for t in task_rows)
                origins = {task_admission(t, mission)['origin'] for t in task_rows}
                hosts = {urlsplit(o).hostname for o in origins if o}
                penalties = [r['data'] for r in conn.execute(select(documents).where(documents.c.collection == 'rate')).mappings()
                             if r['data'].get('blocked_until') and _utc(r['data']['blocked_until']).timestamp() > now
                             and str(r['data'].get('key', '')).rsplit(':', 1)[-1] in hosts]
                rate_seen = max((r['blocked_until'] for r in penalties), default=None)
                target = min(ceiling, max(1, int(row['target'])))
                reason = 'within_approved_limit'
                window_due = row['last_observed_at'] is not None and now - row['last_observed_at'] >= settings.scheduler_interval_seconds
                if window_due and (mission.get('parallelism') or {}).get('mode', 'adaptive') == 'adaptive':
                    adverse = resource_pressure or failures > row['failures_seen'] or (rate_seen and rate_seen != row.get('rate_seen'))
                    if adverse:
                        target = max(1, target // 2); row['stable_windows'] = 0
                        reason = 'resource_pressure' if resource_pressure else 'source_backoff' if penalties else 'recent_failure'
                    elif queued and completed > row['completed_seen'] and not penalties:
                        row['stable_windows'] += 1
                        if row['stable_windows'] >= 3:
                            target = min(ceiling, target + 1); row['stable_windows'] = 0; reason = 'three_stable_progress_windows'
                    else:
                        row['stable_windows'] = 0
                        if penalties: reason = 'source_cooldown'
                network_work = sum(task_admission(t, mission)['executor'] for t in [*running, *queued])
                demand += min(target, network_work)
                profile = self.engine.profile(mission)
                runtime_kind = profile.get('browser_backend') or getattr(settings, 'browser_backend', 'playwright')
                row.update(target=target, approved_max=ceiling, initial=initial, runtime_kind=runtime_kind, running=len(running), runnable=len(queued),
                    ready_executor_capacity=ready_capacity, reason=reason, job_revision=job['revision'],
                    control_epoch=row['control_epoch'] + (int(row.get('target', 0)) != target))
                if row['last_observed_at'] is None or window_due:
                    row.update(last_observed_at=now, completed_seen=completed, failures_seen=failures, rate_seen=rate_seen)
                self.store._put(conn, 'scheduler.job', job['id'], row, job)
            global_row = {'id': 'global', 'global_limit': global_cap, 'executor_limit': ready_capacity if execution.enabled else None,
                          'remote_execution': execution.enabled, 'observed_at': now, 'resource_pressure': resource_pressure,
                          'desired_executors': min(global_cap, int(settings.pool_max_executors), demand + len(pinned)),
                          'ready_executors': len(live), 'pinned_executors': len(pinned),
                          'native_desktop_limit': 1 if getattr(settings, 'desktop_resource_mode', '') == 'watchdog' else None,
                          'native_desktop_capacity_verified': False}
            self.store._put(conn, 'scheduler.control', 'global', global_row)
        resize = getattr(self.engine, 'resize_workers', None)
        if resize:
            total_demand = sum(min(r['target'], r['running'] + r['runnable']) for r in self.store.list_documents('scheduler.job')
                               if self.store.get_job(r['job_id'])['status'] in ('queued', 'running', 'resuming'))
            resize(min(settings.max_workers, global_cap, total_demand))
        workflows = getattr(self.engine, 'workflows', None)
        if workflows:
            for job in current_jobs:
                details = job.get('details') or {}
                if details.get('workflow_run_id'): workflows.reconcile(job['id'])
        return global_row
