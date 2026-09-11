"""Durable elapsed-time challenge policy, independent of model confidence.

Only trusted browser observations enter this module. Legacy episodes retain their
original active-time semantics; creating another browser never renews an episode.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

def canonical_digest(value):
    from .models import canonical_digest as digest
    return digest(value)


def default_policy():
    return {'policy_version': 2, 'mode': 'auto', 'adaptive': True,
            'max_attempts_per_episode': 3, 'max_elapsed_seconds': 120,
            'hard_max_attempts': 6, 'hard_max_elapsed_seconds': 300}


def normalize_policy(value):
    if value is None: return default_policy()
    if not isinstance(value, dict): raise ValueError('on_challenge must be an object')
    result = dict(value)
    if result.get('policy_version') != 2:
        # Explicit historical contracts are not silently given a larger budget.
        for key, default in [('max_attempts_per_episode', 3), ('max_active_seconds', 120)]:
            number = result.get(key, default)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
                raise ValueError('Invalid legacy challenge budget')
        return result
    result = {**default_policy(), **result}
    if result['mode'] not in ('auto', 'manual'): raise ValueError('Invalid challenge mode')
    if not isinstance(result['adaptive'], bool): raise ValueError('adaptive must be boolean')
    for key, ceiling in [('max_attempts_per_episode', 6), ('max_elapsed_seconds', 300),
                         ('hard_max_attempts', 6), ('hard_max_elapsed_seconds', 300)]:
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 < number <= ceiling:
            raise ValueError('Invalid elapsed challenge budget')
        if 'attempts' in key and not isinstance(number, int): raise ValueError('Challenge attempts must be integers')
    if result['max_attempts_per_episode'] > result['hard_max_attempts'] or result['max_elapsed_seconds'] > result['hard_max_elapsed_seconds']:
        raise ValueError('Initial challenge allocation exceeds approved ceiling')
    return result


def _now(): return datetime.now(timezone.utc)
def _time(value): return datetime.fromisoformat(value).astimezone(timezone.utc)
def _elapsed(value, now): return max(0., (now - _time(value['started_at'])).total_seconds())
def _public(value, now):
    return {**value, 'elapsed_seconds': _elapsed(value, now),
            'remaining_seconds': max(0., (_time(value['deadline_at']) - now).total_seconds())}

def _expired(value, now): return now >= _time(value['deadline_at'])
def _save(store, conn, value, job):
    owner_id = value.setdefault('episode_owner_job_id', job['id'])
    owner = job if owner_id == job['id'] else store._job(conn, owner_id)
    store._put(conn, 'challenge', value['id'], value, owner)


def _owner(store, conn, value, fallback):
    value.setdefault('episode_owner_job_id', fallback['id'])
    value.setdefault('episode_owner_revision', fallback['revision'])
    value.setdefault('episode_owner_generation', fallback['generation'])
    return store._job(conn, value['episode_owner_job_id'])


def join_policy(store, conn, value, job, now):
    """Intersect newly admitted policy constraints; never renew a shared episode.

    Called inside the reservation transaction, including the legacy Store branch.
    Active-time participants keep an active-time constraint instead of silently
    translating their duration into an elapsed-time allowance.
    """
    old = store._doc(conn, 'challenge', value['id'])
    owner = store._job(conn, old['job_id']) if old else job
    _owner(store, conn, value, owner)
    policies = dict(value.get('participant_policies', {}))
    applied = set(value.get('applied_policy_digests', []))
    # Migrate an existing episode without treating its owner's original initial
    # allocation as a new restriction after it has already adapted.
    owner_policy = normalize_policy(owner['mission'].get('on_challenge', {}))
    if not applied:
        owner_digest = canonical_digest(owner_policy)
        applied.add(owner_digest); policies.setdefault(owner['id'], owner_digest)
        value.setdefault('policy_initial_attempts', int(owner_policy.get('max_attempts_per_episode', 3)))
        if value.get('clock') == 'elapsed':
            value.setdefault('policy_initial_elapsed_seconds', float(owner_policy.get('max_elapsed_seconds', value['max_elapsed_seconds'])))
    policy = normalize_policy(job['mission'].get('on_challenge', {}))
    digest = canonical_digest(policy)
    policies[job['id']] = digest
    value['participant_policies'] = policies
    if digest in applied:
        value['applied_policy_digests'] = sorted(applied)
        return value
    applied.add(digest); value['applied_policy_digests'] = sorted(applied)
    before = {k:value.get(k) for k in ('mode','adaptive','max_attempts','max_elapsed_seconds','hard_max_attempts','hard_max_elapsed_seconds','legacy_active_limit')}
    manual = policy.get('mode') == 'manual'
    initial_attempts = int(policy.get('max_attempts_per_episode', 3))
    prior_initial_attempts = value.get('policy_initial_attempts', initial_attempts)
    value['policy_initial_attempts'] = min(prior_initial_attempts, initial_attempts)
    if policy.get('policy_version') == 2:
        adaptive = policy['adaptive']
        attempt_ceiling = policy['hard_max_attempts'] if adaptive else initial_attempts
        if value.get('clock') == 'elapsed':
            prior_initial = value.get('policy_initial_elapsed_seconds', value['max_elapsed_seconds'])
            seconds_ceiling = policy['hard_max_elapsed_seconds'] if adaptive else policy['max_elapsed_seconds']
            value['hard_max_attempts'] = min(value['hard_max_attempts'], attempt_ceiling)
            value['hard_max_elapsed_seconds'] = min(value['hard_max_elapsed_seconds'], float(seconds_ceiling))
            if policy['max_elapsed_seconds'] < prior_initial:
                value['max_elapsed_seconds'] = min(value['max_elapsed_seconds'], float(policy['max_elapsed_seconds']))
            value['policy_initial_elapsed_seconds'] = min(prior_initial, float(policy['max_elapsed_seconds']))
            value['max_elapsed_seconds'] = min(value['max_elapsed_seconds'], value['hard_max_elapsed_seconds'])
            value['max_active_seconds'] = value['max_elapsed_seconds']
            value['deadline_at'] = (_time(value['started_at']) + timedelta(seconds=value['max_elapsed_seconds'])).isoformat()
            value['hard_deadline_at'] = (_time(value['started_at']) + timedelta(seconds=value['hard_max_elapsed_seconds'])).isoformat()
        else:
            # Never convert or enlarge an existing active-time episode.
            value['max_attempts'] = min(value['max_attempts'], initial_attempts)
        value['adaptive'] = bool(value.get('adaptive', False) and adaptive)
    else:
        attempt_ceiling = initial_attempts
        active_limit = float(policy.get('max_active_seconds', 120))
        value['legacy_active_limit'] = min(value.get('legacy_active_limit', active_limit), active_limit)
        value['adaptive'] = False
        if value.get('clock') != 'elapsed':
            value['max_active_seconds'] = min(value['max_active_seconds'], active_limit)
        elif value.get('token') and value['active_seconds'] + max(0., (now - _time(value['reserved_at'])).total_seconds()) >= value['legacy_active_limit']:
            value.update(state='awaiting_user', stop_reason='active_time_limit')
    if 'hard_max_attempts' in value:
        value['hard_max_attempts'] = min(value['hard_max_attempts'], attempt_ceiling)
    # Only a genuinely smaller initial policy resets a previously adapted soft
    # allocation; repeating the same approved policy never does.
    if initial_attempts < prior_initial_attempts:
        value['max_attempts'] = min(value['max_attempts'], initial_attempts)
    value['max_attempts'] = min(value['max_attempts'], value.get('hard_max_attempts', attempt_ceiling), attempt_ceiling)
    if manual:
        value.update(mode='manual', state='awaiting_user', stop_reason='manual_policy')
    elif value['attempts'] >= value['max_attempts']:
        value.update(state='awaiting_user', stop_reason='participant_attempt_limit')
    if value.get('legacy_active_limit') is not None and value['active_seconds'] >= value['legacy_active_limit']:
        value.update(state='awaiting_user', stop_reason='active_time_limit')
    after = {k:value.get(k) for k in before}
    if before != after: value['budget_epoch'] = value.get('budget_epoch', 1) + 1
    store._event(conn, job['id'], 'challenge.participant_admitted', {'challenge_id':value['id'],
        'episode_owner_job_id':value['episode_owner_job_id'], 'participant_job_id':job['id'],
        'policy_digest':digest, 'restricted':before != after})
    return value


def _expire_reservation(store, conn, value, now):
    if not value.get('token'): return False
    cutoffs = [_time(value['expires_at']), _time(value['deadline_at'])]
    if value.get('legacy_active_limit') is not None:
        remaining = max(0., value['legacy_active_limit'] - value['active_seconds'])
        cutoffs.append(_time(value['reserved_at']) + timedelta(seconds=remaining))
    cutoff = min(cutoffs)
    if now < cutoff: return False
    charged = max(0., (cutoff - _time(value['reserved_at'])).total_seconds())
    value['active_seconds'] += charged
    reservation_job = value.get('reservation_job_id') or value['episode_owner_job_id']
    value['last_reservation'] = {'job_id':reservation_job, 'status':'expired', 'expired_at':cutoff.isoformat(),
        'original_expires_at':value['expires_at'], 'charged_active_seconds':charged, 'attempt':value['attempts']}
    value.update(token=None, reserved_at=None, expires_at=None, reservation_job_id=None,
                 state='awaiting_user', stop_reason='elapsed_deadline' if _expired(value, now) else 'active_time_limit')
    store._event(conn, reservation_job, 'challenge.reservation_expired', {'challenge_id':value['id'],
        'episode_owner_job_id':value['episode_owner_job_id'], **value['last_reservation']})
    return True


def _enforce_limits(value, now):
    if value['state'] == 'resolved': return
    if _expired(value, now): value.update(state='awaiting_user', stop_reason='elapsed_deadline')
    elif value.get('mode') == 'manual': value.update(state='awaiting_user', stop_reason='manual_policy')
    elif value.get('legacy_active_limit') is not None and value['active_seconds'] >= value['legacy_active_limit']:
        value.update(state='awaiting_user', stop_reason='active_time_limit')
    elif not value.get('token') and value['attempts'] >= value['max_attempts']:
        value.update(state='awaiting_user', stop_reason=value.get('stop_reason') or 'attempt_limit')



def observe(store, job_id, origin, auth_context, observation=None):
    identity = canonical_digest([origin, auth_context])
    with store._tx() as conn:
        job, now = store._job(conn, job_id), _now()
        policy = normalize_policy(job['mission'].get('on_challenge', {}))
        old = store._doc(conn, 'challenge', identity)
        value = dict(old['data']) if old else None
        if value and value['state'] != 'resolved':
            join_policy(store, conn, value, job, now)
            if value.get('clock') == 'elapsed':
                _expire_reservation(store, conn, value, now)
                _enforce_limits(value, now)
            _save(store, conn, value, job)
            return _public(value, now) if value.get('clock') == 'elapsed' else value
        if policy.get('policy_version') != 2: return value
        started = now.isoformat()
        value = dict(id=identity, origin=origin, auth_context=auth_context,
            episode=(value['episode'] + 1) if value else 1, state='detected', attempts=0,
            episode_owner_job_id=job['id'], episode_owner_revision=job['revision'], episode_owner_generation=job['generation'],
            reservation_job_id=None, participant_policies={job['id']:canonical_digest(policy)},
            applied_policy_digests=[canonical_digest(policy)], policy_initial_attempts=policy['max_attempts_per_episode'],
            policy_initial_elapsed_seconds=float(policy['max_elapsed_seconds']),
            active_seconds=0., max_attempts=policy['max_attempts_per_episode'],
            max_active_seconds=float(policy['max_elapsed_seconds']), max_elapsed_seconds=float(policy['max_elapsed_seconds']),
            hard_max_attempts=policy['hard_max_attempts'], hard_max_elapsed_seconds=float(policy['hard_max_elapsed_seconds']),
            adaptive=policy['adaptive'], mode=policy['mode'], clock='elapsed', started_at=started,
            deadline_at=(now+timedelta(seconds=policy['max_elapsed_seconds'])).isoformat(),
            hard_deadline_at=(now+timedelta(seconds=policy['hard_max_elapsed_seconds'])).isoformat(),
            token=None, reserved_at=None, expires_at=None, budget_epoch=1, evidence=None, failures=[],
            environment=(observation or {}).get('environment'), observed_phase=(observation or {}).get('phase'), last_extension_attempt=-1)
        if policy['mode'] == 'manual': value.update(state='awaiting_user', stop_reason='manual_policy')
        if (observation or {}).get('reason') in ('environment_incompatible','authentication_required','rate_limited'):
            value.update(state='awaiting_user',stop_reason=observation['reason'])
        _save(store, conn, value, job)
        store._event(conn, job_id, 'challenge.detected', {'challenge_id':identity, 'clock':'elapsed',
            'deadline_at':value['deadline_at'], 'attempts':0, 'max_attempts':value['max_attempts'], 'phase':value['observed_phase']})
        return _public(value, now)


def reserve(store, job_id, identity):
    from .store import ControlConflict
    with store._tx() as conn:
        job, now = store._job(conn, job_id), _now()
        old = store._doc(conn, 'challenge', identity)
        if not old: raise ControlConflict('Challenge observation is required')
        value = dict(old['data'])
        if value['state'] == 'resolved': return {**_public(value,now),'allowed':False,'reason':'observation_required'}
        join_policy(store, conn, value, job, now)
        _expire_reservation(store, conn, value, now)
        _enforce_limits(value, now)
        if value['state'] == 'resolved':return {**_public(value,now),'allowed':False,'reason':'observation_required'}
        if value.get('token'):
            if _time(value['expires_at']) > now:
                _save(store, conn, value, job)
                return {**_public(value, now), 'allowed':False, 'reason':'attempt_in_flight'}
            value['active_seconds'] += max(0., (_time(value['expires_at']) - _time(value['reserved_at'])).total_seconds())
            value.update(token=None, reserved_at=None, expires_at=None)
        if _expired(value, now) or value['attempts'] >= value['max_attempts'] or value['state'] == 'awaiting_user':
            value.update(state='awaiting_user', stop_reason=value.get('stop_reason') or ('elapsed_deadline' if _expired(value, now) else 'attempt_limit'))
            _save(store, conn, value, job)
            return {**_public(value, now), 'allowed':False, 'reason':'budget_exhausted'}
        import uuid
        expires = _time(value['deadline_at'])
        if value.get('legacy_active_limit') is not None:
            expires = min(expires, now + timedelta(seconds=max(0., value['legacy_active_limit'] - value['active_seconds'])))
        value.update(attempts=value['attempts']+1, state='attempting', token=str(uuid.uuid4()),
                     reservation_job_id=job_id, reservation_job_revision=job['revision'], reservation_job_generation=job['generation'],
                     reserved_at=now.isoformat(), expires_at=expires.isoformat())
        _save(store, conn, value, job)
        store._event(conn, job_id, 'challenge.reserved', {'challenge_id':identity, 'episode':value['episode'],
            'attempt':value['attempts'], 'deadline_at':value['deadline_at'], 'episode_owner_job_id':value['episode_owner_job_id'], 'reservation_job_id':job_id})
        return {**_public(value, now), 'allowed':True}


def _adapt(store, conn, value, job, evidence, now):
    if not value.get('adaptive') or value.get('token') or _expired(value, now) or value.get('mode') != 'auto': return False
    if value.get('stop_reason') in ('environment_incompatible', 'authentication_required', 'rate_limited', 'unchanged_challenge'): return False
    failures = value.get('failures', [])
    if len(failures) >= 2 and failures[-1].get('fingerprint') and failures[-1]['fingerprint'] == failures[-2].get('fingerprint'):
        value.update(state='awaiting_user', stop_reason='unchanged_challenge')
        return False
    progress = evidence.get('verification_accepted') is True and evidence.get('phase') != value.get('observed_phase')
    history = store._doc(conn, 'challenge.history', value['id'])
    comparable = [row for row in (history['data'].get('successes', []) if history else []) if row.get('environment') == value.get('environment')]
    slow_success = len(comparable) >= 5 and any(row['attempts'] > value['max_attempts'] or row['elapsed_seconds'] > value['max_elapsed_seconds'] for row in comparable[-10:])
    if not (progress or slow_success): return False
    if value.get('last_extension_attempt') == value['attempts']: return False
    if value['max_attempts'] >= value['hard_max_attempts'] and value['max_elapsed_seconds'] >= value['hard_max_elapsed_seconds']: return False
    # Extend near exhaustion, not immediately on every intermediate observation.
    if value['attempts'] < value['max_attempts'] and (_time(value['deadline_at'])-now).total_seconds() > 30: return False
    value.update(max_attempts=min(value['hard_max_attempts'],value['max_attempts']+1),
        max_elapsed_seconds=min(value['hard_max_elapsed_seconds'],value['max_elapsed_seconds']+60),
        budget_epoch=value['budget_epoch']+1, last_extension_attempt=value['attempts'], state='detected')
    value['max_active_seconds']=value['max_elapsed_seconds']
    value['deadline_at']=(_time(value['started_at'])+timedelta(seconds=value['max_elapsed_seconds'])).isoformat()
    store._event(conn, job['id'], 'challenge.budget_adapted', {'challenge_id':value['id'],
        'reason':'observed_verification_progress' if progress else 'comparable_verified_recoveries',
        'max_attempts':value['max_attempts'], 'deadline_at':value['deadline_at'], 'budget_epoch':value['budget_epoch']})
    return True


def finish(store, identity, token, active_seconds=0, resolved=False, evidence=None):
    from .store import LeaseLost
    if not math.isfinite(active_seconds) or active_seconds < 0: raise ValueError('active_seconds must be nonnegative')
    if resolved and not evidence: raise ValueError('Resolution requires observed evidence')
    with store._tx() as conn:
        old=store._doc(conn,'challenge',identity)
        if not old or not token or old['data'].get('token') != token: raise LeaseLost('Challenge reservation no longer owned')
        value=dict(old['data']);owner=_owner(store,conn,value,store._job(conn,old['job_id']));
        job=store._job(conn,value.get('reservation_job_id') or owner['id']);now=_now();evidence=dict(evidence or {})
        value['active_seconds']+=max(float(active_seconds),(now-_time(value['reserved_at'])).total_seconds())
        value['last_reservation']={'job_id':job['id'],'status':'finished','finished_at':now.isoformat(),'attempt':value['attempts']}
        value.update(token=None,reserved_at=None,expires_at=None,reservation_job_id=None,evidence=evidence)
        # Recovery observed after the elapsed limit may be adopted later by a
        # trusted human observation, but is not an in-budget automatic success.
        if resolved and not _expired(value,now):
            value.update(state='resolved',stop_reason=None)
            history=store._doc(conn,'challenge.history',identity)
            successes=list(history['data'].get('successes',[])) if history else []
            successes.append({'environment':value.get('environment'),'attempts':value['attempts'],'elapsed_seconds':_elapsed(value,now),'reservation_job_id':job['id'],'episode_owner_job_id':owner['id']})
            store._put(conn,'challenge.history',identity,{'id':identity,'successes':successes[-30:]},owner)
        else:
            value.update(state='detected')
            failures=list(value.get('failures',[]));failures.append({key:evidence.get(key) for key in ('fingerprint','phase','verification_accepted','reason')})
            value['failures']=failures[-6:]
            if evidence.get('reason') in ('environment_incompatible','authentication_required','rate_limited'):
                value.update(state='awaiting_user',stop_reason=evidence['reason'])
            _adapt(store,conn,value,job,evidence,now)
            _enforce_limits(value,now)
        _save(store,conn,value,job)
        store._event(conn,job['id'],'challenge.finished',{'challenge_id':identity,'state':value['state'],
            'attempts':value['attempts'],'elapsed_seconds':_elapsed(value,now),'deadline_at':value['deadline_at'],'reason':value.get('stop_reason'),
            'episode_owner_job_id':owner['id'],'reservation_job_id':job['id']})
        return _public(value,now)


def adapt(store, identity, evidence=None):
    with store._tx() as conn:
        old=store._doc(conn,'challenge',identity)
        if not old: raise KeyError(identity)
        value=dict(old['data'])
        if value.get('clock')!='elapsed':return value
        job=_owner(store,conn,value,store._job(conn,old['job_id']));now=_now()
        if value['state']=='resolved':return _public(value,now)
        _expire_reservation(store,conn,value,now)
        if _expired(value,now):value.update(state='awaiting_user',stop_reason='elapsed_deadline')
        else:_adapt(store,conn,value,job,evidence or {},now)
        _save(store,conn,value,job)
        return _public(value,now)


def extend(store, identity, attempts, seconds, authorizer):
    from .store import ControlConflict
    if not authorizer or attempts<0 or seconds<0:raise ValueError('Explicit authorizer and nonnegative extension required')
    with store._tx() as conn:
        old=store._doc(conn,'challenge',identity)
        if not old:raise KeyError(identity)
        value=dict(old['data']);now=_now()
        _owner(store,conn,value,store._job(conn,old['job_id']))
        if value.get('token'):raise ControlConflict('Cannot extend an active attempt')
        if _expired(value,now):raise ControlConflict('Elapsed challenge episode has ended')
        if value['max_attempts']+attempts>value['hard_max_attempts'] or value['max_elapsed_seconds']+seconds>value['hard_max_elapsed_seconds']:
            raise ControlConflict('Challenge extension exceeds approved ceiling')
        value.update(max_attempts=value['max_attempts']+attempts,max_elapsed_seconds=value['max_elapsed_seconds']+seconds,
                     budget_epoch=value['budget_epoch']+1,state='detected')
        value['max_active_seconds']=value['max_elapsed_seconds']
        value['deadline_at']=(_time(value['started_at'])+timedelta(seconds=value['max_elapsed_seconds'])).isoformat()
        job=store._job(conn,old['job_id']);_save(store,conn,value,job)
        store._event(conn,job['id'],'challenge.budget_extended',{'challenge_id':identity,'authorized_by':authorizer,
            'extra_attempts':attempts,'extra_seconds':seconds,'deadline_at':value['deadline_at']})
        return _public(value,now)
