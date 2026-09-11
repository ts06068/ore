"""Coordinator side of the trusted, outbound host provisioning channel.

The pool role can report capacity and receive single-use worker enrollments. It
cannot read profiles, submit tools, claim model tasks or administer missions.
"""
from __future__ import annotations

import hashlib
import secrets
import time
import uuid

from fastapi import APIRouter, Request
from .policy import AccessDenied
from .store import documents
from sqlalchemy import select


class PoolManager:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.token = engine.settings.pool_token

    def authorize_request(self, request):
        header = request.headers.get('authorization', '')
        token = header[7:] if header.lower().startswith('bearer ') else ''
        return bool(request.method == 'POST' and request.url.path == '/v1/pool/report'
                    and self.token and secrets.compare_digest(token, self.token))

    def valid_enrollment(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.store.get_document('pool.launch', digest)
        return bool(row and not row.get('consumed_at') and row['expires_at'] > time.time())

    def consume_enrollment(self, token, worker_id, network_zone):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store._tx() as conn:
            raw = self.store._doc(conn, 'pool.launch', digest)
            if not raw or raw['data'].get('consumed_at') or raw['data']['expires_at'] <= time.time():
                raise AccessDenied('Executor launch enrollment expired or was already consumed')
            row = raw['data']
            if row['worker_id'] != worker_id or row['network_zone'] != network_zone:
                raise AccessDenied('Launch enrollment is bound to another executor or network zone')
            self.store._put(conn, 'pool.launch', digest, {**row, 'consumed_at': time.time()})
            return row

    async def report(self, data):
        import math
        broker = str(data.get('broker_id', ''))
        if not broker or len(broker) > 80 or not all(c.isalnum() or c in '-_' for c in broker):
            raise AccessDenied('Invalid host broker identity')
        capacity = data.get('capacity', 0)
        if type(capacity) is not int or capacity < 0 or capacity > 64:
            raise AccessDenied('Invalid host capacity')
        resources = {k: float(v) for k, v in (data.get('resources') or {}).items()
                     if k in ('cpu_ratio', 'memory_ratio', 'available_memory_bytes', 'logical_cpus')}
        if any(not math.isfinite(v) or v < 0 for v in resources.values()):
            raise AccessDenied('Invalid host resource observation')
        containers = data.get('containers', [])
        if not isinstance(containers, list) or len(containers) > 64:
            raise AccessDenied('Invalid host container inventory')
        inventory = {str(c.get('worker_id')): {'worker_id': str(c.get('worker_id')), 'state': str(c.get('state', 'unknown')),
                     'container_id': str(c.get('container_id', ''))[:64]} for c in containers}
        now = time.time()
        self.store.put_document('pool.broker', broker, {'id': broker, 'capacity': capacity, 'resources': resources,
            'containers': list(inventory.values()), 'observed_at': now, 'resource_mode': data.get('resource_mode', 'unknown')})
        # Multiple hosts share the global desired fleet, never each receive that total.
        brokers = [b for b in self.store.list_documents('pool.broker') if now - b['observed_at'] < 40]
        total_capacity = min(self.engine.settings.pool_max_executors, sum(b['capacity'] for b in brokers))
        self.store.put_document('pool.state', 'fleet', {'id': 'fleet', 'capacity': total_capacity,
            'resources': {'cpu_ratio': max((b['resources'].get('cpu_ratio', 0) for b in brokers), default=0),
                          'memory_ratio': max((b['resources'].get('memory_ratio', 0) for b in brokers), default=0)},
            'observed_at': now, 'brokers': len(brokers)})
        execution = self.engine.execution
        launches, drains = [], []
        async with execution.lock:
            desired = min((self.store.get_document('scheduler.control', 'global') or {}).get('desired_executors', 0), total_capacity)
            workers = execution._list('worker')
            live = [w for w in workers if w.get('state') != 'lost' and now - w.get('last_seen', 0) < 40]
            for worker in live:
                if worker.get('pool_broker_id') == broker and worker['id'] in inventory and inventory[worker['id']]['state'] in ('exited', 'dead', 'absent'):
                    await execution.worker_lost(worker['id'], 'host_confirmed_container_stopped')
            live = [w for w in execution._list('worker') if w.get('state') != 'lost' and now - w.get('last_seen', 0) < 40]
            with self.store._tx() as conn:
                pending = [r['data'] for r in conn.execute(select(documents).where(documents.c.collection == 'pool.launch')).mappings()
                           if not r['data'].get('consumed_at') and r['data']['expires_at'] > now]
                local_live = [w for w in live if w.get('pool_broker_id') == broker]
                local_pending = [p for p in pending if p['broker_id'] == broker]
                physical = {worker for worker, item in inventory.items() if item['state'] not in ('absent', 'exited', 'dead')}
                occupied = physical | {w['id'] for w in local_live} | {p['worker_id'] for p in local_pending}
                count = min(max(0, desired - len(live) - len(pending)), max(0, capacity - len(occupied)))
                for _ in range(count):
                    token = secrets.token_urlsafe(40); worker_id = 'pool-' + broker + '-' + uuid.uuid4().hex[:12]
                    row = {'worker_id': worker_id, 'broker_id': broker, 'network_zone': execution.network_zone,
                           'created_at': now, 'expires_at': now + 120, 'consumed_at': None}
                    self.store._put(conn, 'pool.launch', hashlib.sha256(token.encode()).hexdigest(), row)
                    launches.append({**row, 'enrollment_token': token})
            excess = max(0, len(live) - desired)
            sessions = execution._list('session')
            assignments = execution._list('assignment')
            commands = execution._list('command')
            for worker in sorted(local_live, key=lambda w: w.get('idle_since', now)):
                if not excess: break
                ident = worker['id']
                if worker['state'] not in ('idle', 'draining'): continue
                if now - worker.get('idle_since', worker.get('last_seen', now)) < self.engine.settings.pool_idle_seconds: continue
                if any(s['worker_id'] == ident and not s.get('closed') for s in sessions): continue
                if any(a['worker_id'] == ident and a['status'] == 'active' for a in assignments): continue
                if any(c['worker_id'] == ident and c['status'] in ('queued', 'running', 'cancel_requested') for c in commands): continue
                execution._put('worker', ident, {**worker, 'state': 'draining'})
                drains.append({'worker_id': ident, 'reason': 'idle_above_demand'}); excess -= 1
        return {'launch': launches, 'drain': drains, 'desired': desired, 'capacity': total_capacity,
                'ready': sum(w['state'] in ('idle', 'busy') for w in live), 'poll_after_seconds': 5}


def create_pool_router(manager):
    router = APIRouter(prefix='/v1/pool')
    @router.post('/report')
    async def report(request: Request):
        if not manager.authorize_request(request): raise AccessDenied('Invalid host pool role')
        return await manager.report(await request.json())
    return router
