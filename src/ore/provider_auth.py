"""Official account adapters. Credentials stay inside the provider CLI.

Codex protocol: https://developers.openai.com/codex/app-server
Only device-code start/cancel and read-only account/usage operations are exposed.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import time
from urllib.parse import urlsplit


def observed_at():
    return datetime.now(timezone.utc).isoformat()


def unknown_quota(status='unknown'):
    return {'status': status, 'windows': [], 'ordinary_usage_allowed': None, 'stale': False}


def quota_snapshot(raw):
    buckets = raw.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        buckets = {'codex': raw.get('rateLimits') or {}}
    windows = []
    for ident, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for name in ('primary', 'secondary'):
            value = bucket.get(name)
            if not isinstance(value, dict) or type(value.get('usedPercent')) not in (int, float):
                continue
            percent = value['usedPercent']
            if not 0 <= percent <= 100:
                continue
            window = {'id': str(ident)[:120] + ':' + name, 'used_percent': percent,
                      'remaining_percent': 100 - percent}
            for source, target in [('windowDurationMins', 'window_minutes'), ('resetsAt', 'resets_at')]:
                if type(value.get(source)) in (int, float):
                    window[target] = value[source]
            windows.append(window)
    allowed = raw.get('ordinaryUsageAllowed')
    return {'status': 'available' if windows else 'unknown', 'windows': windows,
            'ordinary_usage_allowed': allowed if type(allowed) is bool else None,
            'observed_at': observed_at(), 'stale': False}


class CodexAuth:
    def __init__(self, backend):
        self.backend = backend
        self._auth = None
        self._quota = None
        self._raw_quota = None
        self._auth_time = self._quota_time = 0.0
        self._login = None
        self._login_callback = None
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._closed = False
        if hasattr(backend, 'add_account_listener'):
            backend.add_account_listener(self._event)

    def _event(self, method, params):
        if self._closed:
            return
        if method == 'account/rateLimits/updated':
            # A sparse update has no account identity/recovery authority. Merge
            # only its non-null bucket fields into an existing full snapshot.
            if self._raw_quota is None:
                self._quota_time = 0
                return
            value = params.get('rateLimits')
            if isinstance(value, dict):
                old = deepcopy(self._raw_quota)
                ident = value.get('limitId')
                default = old.get('rateLimits') or {}
                if not ident or not default.get('limitId') or ident == default.get('limitId'):
                    old['rateLimits'] = {**default, **{key: item for key, item in value.items() if item is not None}}
                if ident and isinstance(old.get('rateLimitsByLimitId'), dict):
                    old['rateLimitsByLimitId'][ident] = {**old['rateLimitsByLimitId'].get(ident, {}),
                                                       **{key: item for key, item in value.items() if item is not None}}
                self._raw_quota = old
                self._quota = quota_snapshot(old)
                self._quota_time = time.monotonic()
        elif method == 'account/updated':
            self._auth_time = self._quota_time = 0
            self._raw_quota = None
            self._quota = None
        elif method == 'account/login/completed':
            login = self._login
            if not login or params.get('loginId') != login['login_id']:
                return
            status = 'ready' if params.get('success') is True else 'failed'
            self._login = {'login_id': login['login_id'], 'status': status}
            self._auth_time = self._quota_time = 0
            if self._login_callback:
                return self._login_callback(dict(self._login))

    async def auth_status(self, *, refresh=False):
        if not getattr(self.backend, 'binary', True):
            return {'status': 'not_installed', 'observed_at': observed_at()}
        if not refresh and self._auth is not None and time.monotonic() - self._auth_time < 30:
            return deepcopy(self._auth)
        async with self._refresh_lock:
            try:
                raw = await self.backend.account_request('account/read', {'refreshToken': False})
                account = raw.get('account')
                result = {'status': 'ready' if isinstance(account, dict) else 'not_authenticated',
                          'observed_at': observed_at()}
                if isinstance(account, dict):
                    if account.get('type') in {'chatgpt', 'apiKey', 'amazonBedrock'}:
                        result['mode'] = account['type']
                    if isinstance(account.get('planType'), str):
                        result['plan_type'] = account['planType'][:80]
                self._auth, self._auth_time = result, time.monotonic()
                return deepcopy(result)
            except Exception:
                return {'status': 'unknown', 'message_code': 'account_status_unavailable', 'observed_at': observed_at()}

    async def start_login(self, on_event=None):
        async with self._lock:
            if self._login and self._login['status'] in {'awaiting_auth', 'cancel_unconfirmed'}:
                raise ValueError('Another Codex sign-in is already pending')
            if (await self.auth_status(refresh=True))['status'] == 'ready':
                return {'status': 'ready', 'message_code': 'existing_account_connected'}
            raw = await self.backend.account_request('account/login/start', {'type': 'chatgptDeviceCode'})
            parsed = urlsplit(raw.get('verificationUrl', ''))
            if (raw.get('type') != 'chatgptDeviceCode' or not raw.get('loginId')
                    or parsed.scheme != 'https' or parsed.hostname not in {'auth.openai.com', 'auth0.openai.com', 'chatgpt.com'}
                    or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or not isinstance(raw.get('userCode'), str) or not 1 <= len(raw['userCode']) <= 128):
                if raw.get('loginId'):
                    await self.backend.account_request('account/login/cancel', {'loginId': raw['loginId']})
                raise ValueError('Provider returned an unsupported official sign-in response')
            self._login = {'status': 'awaiting_auth', 'login_id': raw['loginId'],
                           'verification_url': raw['verificationUrl'], 'user_code': raw['userCode']}
            self._login_callback = on_event
            return dict(self._login)

    def login_status(self, login_id):
        if self._login and self._login['login_id'] == login_id:
            return dict(self._login)
        return {'status': 'interrupted', 'login_id': login_id}

    async def cancel_login(self, login_id):
        async with self._lock:
            if not self._login or self._login.get('login_id') != login_id:
                return {'acknowledged': False, 'status': 'cancel_unconfirmed'}
            if self._login['status'] in {'cancelled', 'failed', 'ready'}:
                return {'acknowledged': True, 'status': self._login['status']}
            raw = await self.backend.account_request('account/login/cancel', {'loginId': login_id})
            confirmed = raw.get('status') == 'canceled'
            status = 'cancelled' if confirmed else 'cancel_unconfirmed'
            self._login = {'login_id': login_id, 'status': status}
            return {'acknowledged': confirmed, 'status': status}

    async def quota_status(self, *, refresh=False):
        if not refresh and self._quota is not None and time.monotonic() - self._quota_time < 30:
            return deepcopy(self._quota)
        try:
            raw = await self.backend.account_request('account/rateLimits/read')
            # Exclude accountId, upsells and any credential-like metadata.
            self._raw_quota = {key: raw[key] for key in ('rateLimits', 'rateLimitsByLimitId', 'ordinaryUsageAllowed') if key in raw}
            self._quota = quota_snapshot(self._raw_quota)
            self._quota_time = time.monotonic()
            return deepcopy(self._quota)
        except Exception:
            return {**deepcopy(self._quota), 'status': 'stale', 'stale': True} if self._quota else unknown_quota('unsupported')

    async def usage_status(self):
        try:
            raw = await self.backend.account_request('account/tokenUsage/read')
            summary = raw.get('summary') or {}
            public = {key: summary[key] for key in ('lifetimeTokens', 'peakDailyTokens', 'currentStreakDays')
                      if type(summary.get(key)) is int and summary[key] >= 0}
            return {'status': 'available' if public else 'unknown', 'summary': public,
                    'observed_at': observed_at(), 'scope': 'provider_account_activity_not_request_budget'}
        except Exception:
            return {'status': 'unsupported'}

    async def close(self):
        self._closed = True
        if self._login and self._login['status'] == 'awaiting_auth':
            try:
                await self.cancel_login(self._login['login_id'])
            except Exception:
                pass
        if hasattr(self.backend, 'remove_account_listener'):
            self.backend.remove_account_listener(self._event)
