"""Durable shared accounting for planning, native execution and repair.

Provider tokens are observations, not invoices. An in-flight completion may exceed
its allowance; accounting records that excess and forbids subsequent work.
"""
from __future__ import annotations

from copy import deepcopy
import time
from typing import Callable


class BudgetExhausted(RuntimeError):
    def __init__(self, code: str, snapshot: dict):
        super().__init__(code)
        self.code, self.snapshot = code, snapshot


class RunBudget:
    collection = 'workflow.budget'

    def __init__(self, store, scope_id: str, limits: dict | None = None, *, clock: Callable = time.time):
        self.store, self.scope_id, self.clock = store, str(scope_id), clock
        with store._tx() as conn:
            row = store._doc(conn, self.collection, self.scope_id)
            if not row:
                store._put(conn, self.collection, self.scope_id, {
                    'id': self.scope_id, 'schema_version': 'ore.budget/v2',
                    'limits': deepcopy(limits or {}), 'active_since': clock(), 'active_seconds': 0.0,
                    'paused': False, 'provider_turns': 0, 'tokens': {}, 'watermarks': {},
                    'invocations': {}, 'unknown_sessions': [], 'counter_regressions': [], 'bytes': 0})

    def _read(self, conn):
        row = self.store._doc(conn, self.collection, self.scope_id)
        if not row:
            raise KeyError(self.scope_id)
        return deepcopy(row['data'])

    def _write(self, conn, state):
        self.store._put(conn, self.collection, self.scope_id, state)

    @staticmethod
    def _key(provider, session):
        import json
        return json.dumps([str(provider), str(session)], separators=(',', ':'))

    @staticmethod
    def _counts(value):
        aliases = {'total_tokens': 'totalTokens', 'input_tokens': 'inputTokens',
                   'output_tokens': 'outputTokens', 'cached_input_tokens': 'cachedInputTokens'}
        result = {}
        for key, count in (value or {}).items():
            if isinstance(count, (int, float)) and not isinstance(count, bool) and count >= 0:
                result[aliases.get(key, key)] = int(count)
        if 'totalTokens' not in result and {'inputTokens', 'outputTokens'} <= result.keys():
            result['totalTokens'] = result['inputTokens'] + result['outputTokens']
        return result

    def _snapshot(self, state):
        result = deepcopy(state)
        elapsed = state['active_seconds']
        if state.get('active_since') is not None:
            elapsed += max(0.0, self.clock() - state['active_since'])
        result['elapsed_seconds'] = elapsed
        limits = state['limits']
        result['remaining_seconds'] = max(0.0, limits['max_seconds'] - elapsed) if limits.get('max_seconds') is not None else None
        total = state['tokens'].get('totalTokens', 0)
        result['remaining_tokens'] = max(0, limits['max_tokens'] - total) if limits.get('max_tokens') is not None else None
        result['usage_complete'] = not state['unknown_sessions'] and not state['counter_regressions']
        # An explicitly unstarted run has known zero usage, rather than missing usage.
        result['zero_model_calls'] = state['provider_turns'] == 0
        result['byte_overshoot'] = max(0, state['bytes'] - limits['max_bytes']) if limits.get('max_bytes') is not None else 0
        result['token_overshoot'] = max(0, total - limits['max_tokens']) if limits.get('max_tokens') is not None else 0
        return result

    def snapshot(self):
        row = self.store.get_document(self.collection, self.scope_id)
        if row is None:
            raise KeyError(self.scope_id)
        return self._snapshot(row)

    def _check(self, snapshot, *, model=False):
        limits = snapshot['limits']
        if snapshot['paused']:
            raise BudgetExhausted('budget_paused', snapshot)
        if not snapshot['usage_complete']:
            raise BudgetExhausted('budget_accounting_unavailable', snapshot)
        if snapshot['remaining_seconds'] is not None and snapshot['remaining_seconds'] <= 0:
            raise BudgetExhausted('budget_time_exhausted', snapshot)
        if snapshot['remaining_tokens'] is not None and snapshot['remaining_tokens'] <= 0:
            raise BudgetExhausted('budget_token_exhausted', snapshot)
        if limits.get('max_bytes') is not None and snapshot['bytes'] >= limits['max_bytes']:
            raise BudgetExhausted('budget_bytes_exhausted', snapshot)
        if model and limits.get('max_turns') is not None and snapshot['provider_turns'] >= limits['max_turns']:
            raise BudgetExhausted('budget_turn_exhausted', snapshot)

    def authorize(self, phase='execution'):
        snapshot = self.snapshot()
        self._check(snapshot, model=phase in ('model', 'planning', 'repair', 'calibration'))
        return snapshot

    def begin_model(self, invocation_id, phase='execution'):
        """Charge each admitted provider transport invocation exactly once."""
        with self.store._tx() as conn:
            state = self._read(conn)
            if invocation_id in state['invocations']:
                return self._snapshot(state)
            self._check(self._snapshot(state), model=True)
            state['provider_turns'] += 1
            state['invocations'][invocation_id] = {'phase': phase, 'started_at': self.clock()}
            self._write(conn, state)
            return self._snapshot(state)

    def bind_session(self, provider, session, cumulative=None):
        """Seed prior history only when a session joins a NEW scope; never reset it."""
        key = self._key(provider, session)
        with self.store._tx() as conn:
            state = self._read(conn)
            if key not in state['watermarks']:
                state['watermarks'][key] = self._counts(cumulative)
                self._write(conn, state)
            return self._snapshot(state)

    def observe(self, provider, session, cumulative):
        key = self._key(provider, session)
        counts = self._counts(cumulative)
        if 'totalTokens' not in counts:
            return self.mark_usage_incomplete(provider, session)
        with self.store._tx() as conn:
            state = self._read(conn)
            old = state['watermarks'].get(key, {})
            regressions = [field for field in ('totalTokens', 'inputTokens', 'outputTokens')
                           if field in old and field in counts and counts[field] < old[field]]
            if regressions:
                item = {'session': key, 'fields': regressions}
                if item not in state['counter_regressions']:
                    state['counter_regressions'].append(item)
            else:
                for field, count in counts.items():
                    state['tokens'][field] = state['tokens'].get(field, 0) + max(0, count - old.get(field, 0))
                state['watermarks'][key] = {**old, **counts}
                state['unknown_sessions'] = [value for value in state['unknown_sessions'] if value != key]
            self._write(conn, state)
            return self._snapshot(state)

    def mark_counter_regression(self, provider, session, fields=None):
        """Retain a provider-reported regression even if transport clamps counters."""
        key = self._key(provider, session)
        with self.store._tx() as conn:
            state = self._read(conn)
            item = {'session': key, 'fields': list(fields or ['totalTokens'])}
            if item not in state['counter_regressions']:
                state['counter_regressions'].append(item)
            self._write(conn, state)
            return self._snapshot(state)

    def mark_usage_incomplete(self, provider, session):
        key = self._key(provider, session)
        with self.store._tx() as conn:
            state = self._read(conn)
            if key not in state['unknown_sessions']:
                state['unknown_sessions'].append(key)
            self._write(conn, state)
            return self._snapshot(state)

    def observe_bytes(self, amount, *, observation_id=None):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError('Byte count must be a nonnegative integer')
        with self.store._tx() as conn:
            state = self._read(conn)
            if observation_id is not None:
                seen = state.setdefault('byte_observations', {})
                if observation_id in seen:
                    if seen[observation_id] != amount: raise ValueError('Byte observation changed')
                    return self._snapshot(state)
                seen[observation_id] = amount
            state['bytes'] += amount
            self._write(conn, state)
            return self._snapshot(state)

    def pause(self):
        """Call only after provider and execution interruption are confirmed."""
        with self.store._tx() as conn:
            state = self._read(conn)
            if state.get('active_since') is not None:
                state['active_seconds'] += max(0.0, self.clock() - state['active_since'])
            state.update(active_since=None, paused=True)
            self._write(conn, state)
            return self._snapshot(state)

    def resume(self):
        with self.store._tx() as conn:
            state = self._read(conn)
            candidate = self._snapshot(state)
            candidate['paused'] = False
            self._check(candidate)
            if state['paused']:
                state.update(active_since=self.clock(), paused=False)
                self._write(conn, state)
            return self._snapshot(state)

    def set_limits(self, limits, *, approved=False):
        """Changing allowances preserves all consumed usage and elapsed time."""
        with self.store._tx() as conn:
            state = self._read(conn)
            for key, value in limits.items():
                old = state['limits'].get(key)
                if not approved and old is not None and (value is None or value > old):
                    raise ValueError('Increasing a shared budget requires an approved envelope')
            state['limits'].update(deepcopy(limits))
            self._write(conn, state)
            return self._snapshot(state)
