"""Conversation-linked connection cards, official login, and provider quota.

Cards and action receipts are durable; passwords and issued keys are stored only
through the protected secret endpoint. Device codes live only in process memory.
No login/bootstrap operation depends on an LLM.
"""
from __future__ import annotations
import asyncio
from contextlib import suppress
import inspect
import uuid
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from .policy import AccessDenied
from .provider_auth import CodexAuth, observed_at, unknown_quota
from .source_policy import canonical_operation, canonical_source, source_catalog, source_readiness
from .store import DocumentConflict


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = Field(min_length=1, max_length=80)
    conversation_id: str | None = Field(default=None, max_length=128)
    access_profile_ref: str = Field(default='public', max_length=128)
    operation: str = 'search'


class ConnectionAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9._:-]{4,120}$')
    agent_backend: str = 'codex'
    agent_model: str | None = Field(default=None, max_length=120)


class ConnectionSecret(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str
    value: str = Field(min_length=1, max_length=65536, repr=False)
    credential_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9._:-]{4,120}$')


class LoginCode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=1, max_length=8192, repr=False)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9._:-]{4,120}$')


def provider_name(value):
    return 'claude_code' if value in {'claude', 'claude-code'} else value


class ConnectionManager:
    collection = 'connection'

    def __init__(self, engine, browser):
        self.engine, self.store, self.browser = engine, engine.store, browser
        self.locks = {}
        self.live_logins = {}
        self._closed = False
        # Interrupted effects are recorded for review, never automatically replayed.
        for row in self.store.list_documents(self.collection):
            if row.get('inflight'):
                effect = row['inflight']
                receipts = {**row.get('receipts', {}), effect['key']: {
                    'action': effect['action'], 'status': 'interrupted_outcome_unknown', 'observed_at': observed_at()}}
                self._save(row, {'inflight': None, 'receipts': receipts, 'status': 'needs_review',
                                 'message_code': 'connection_process_restarted_inspect_before_retry'})
        if not hasattr(engine, 'provider_auth'):
            engine.provider_auth = {}
        if 'codex' not in engine.provider_auth:
            engine.provider_auth['codex'] = CodexAuth(engine.backend)
        if 'claude_code' not in engine.provider_auth:
            try:
                from .claude import ClaudeAuth
                engine.provider_auth['claude_code'] = ClaudeAuth(engine.settings.state_dir)
            except ImportError:
                pass

    def _read(self, ident):
        value = self.store.get_document(self.collection, ident)
        if not value:
            raise KeyError('Unknown connection')
        if value.get('linked_job_id'):
            value['job_id'] = value['linked_job_id']
        return value

    def _save(self, row, changes):
        value = {**row, **changes}
        if value.get('job_id'):
            value['linked_job_id'] = value['job_id']
        saved = self.store.put_document(self.collection, row['id'], value, expected_version=row['state_version'])
        if saved.get('linked_job_id'):
            saved['job_id'] = saved['linked_job_id']
        return saved

    def _conversation(self, ident):
        if ident and self.store.get_document('conversation', ident) is None:
            raise KeyError('Unknown conversation')

    def public(self, row):
        keys = {'id', 'provider', 'kind', 'conversation_id', 'access_profile_ref', 'operation',
                'status', 'state_version', 'job_id', 'handoff_id', 'handoff_href', 'workflow_run_id',
                'credential_fields', 'message_code', 'created_at', 'updated_at', 'observed_at'}
        result = {key: row[key] for key in keys if key in row}
        if row.get('linked_job_id'):
            result['job_id'] = row['linked_job_id']
        from .connection_intent import credential_fields
        result['credential_fields'] = credential_fields(row['provider'], row['kind'])
        result['configured_fields'] = sorted(row.get('secret_refs', {}))
        result['credential_pool_allowed'] = row['kind'] == 'source' and row['provider'] != 'pubmed' and 'api_key' in result['credential_fields']
        if row['kind'] == 'source':
            profile = self.engine.profile({'access_profile_ref': row['access_profile_ref']})
            config = profile.get('sources', {}).get(row['provider'], {})
            entries = config.get('credential_pool') or ([{'id': 'primary'}] if config.get('api_key_ref') else [])
            result['credential_pool'] = [{'id': item['id'], 'enabled': item.get('enabled') is not False}
                for item in entries if isinstance(item, dict) and isinstance(item.get('id'), str)]

        if row['provider'] in {'openai', 'anthropic'}:
            result['actions'] = ['refresh', 'cancel']
            if row['status'] != 'cancelled' and row.get('secret_refs', {}).get('api_key'):
                result['backend'] = {'kind': row['provider'], 'api_key_ref': row['secret_refs']['api_key']}
            return result
        if row['kind'] == 'model':
            result['actions'] = ['refresh', 'cancel']
            if row['status'] not in {'ready', 'connecting', 'awaiting_auth', 'cancelling', 'cancel_unconfirmed'}:
                result['actions'].append('start_login')
            if row['status'] == 'awaiting_auth':
                if row['id'] in self.live_logins:
                    result.update(self.live_logins[row['id']])
                    if row['provider'] == 'claude_code':
                        result['login_code_required'] = True
                else:
                    result.update(status='interrupted', message_code='login_process_restarted')
                    result['actions'].append('start_login')
        else:
            result['actions'] = ['open_provider', 'refresh', 'mark_pending', 'cancel', 'request_agent']
            if row['operation'] == 'search':
                result['actions'].append('verify')
            for job_id in (row.get('agent_job_id'), row.get('linked_job_id') or row.get('job_id')):
                if not job_id:
                    continue
                handoffs = self.engine.handoffs.list(job_id, 'active')
                if handoffs:
                    result.update(handoff_id=handoffs[-1]['id'], handoff_href=handoffs[-1]['href'])
                    break
            if row.get('workflow_run_id'):
                with suppress(KeyError):
                    result['workflow_status'] = self.engine.workflows.get_run(row['workflow_run_id'])['status']
        return result

    def list(self, conversation_id=None):
        self._conversation(conversation_id)
        return [self.public(row) for row in self.store.list_documents(self.collection)
                if conversation_id is None or row.get('conversation_id') == conversation_id
                or row['kind'] == 'model' and row.get('conversation_id') is None]

    def create(self, values):
        self._conversation(values.get('conversation_id'))
        provider = provider_name(values['provider'])
        kind = 'model' if provider in {'codex', 'claude_code', 'openai', 'anthropic'} else 'source'
        profile = self.engine.profile({'access_profile_ref': values.get('access_profile_ref', 'public')})
        if kind == 'source':
            provider = canonical_source(provider)
            if provider not in {item['id'] for item in source_catalog(profile, self.engine.secrets.get)}:
                raise ValueError('Unknown source provider')
        operation = canonical_operation(values.get('operation', 'search'))
        for existing in self.store.list_documents(self.collection):
            if (existing['provider'], existing.get('conversation_id'), existing.get('operation')) == (
                    provider, values.get('conversation_id'), operation) and profile['id'] in {
                    existing.get('requested_profile_ref'), existing.get('access_profile_ref')} and existing['status'] != 'cancelled':
                return self.public(existing)
        from .connection_intent import credential_fields
        ident = str(uuid.uuid4())
        value = {'id': ident, 'provider': provider, 'kind': kind, 'status': 'not_connected',
                 'conversation_id': values.get('conversation_id'), 'access_profile_ref': profile['id'], 'requested_profile_ref': profile['id'],
                 'operation': operation, 'credential_fields': credential_fields(provider, kind),
                 'secret_refs': {}, 'receipts': {}, 'inflight': None}
        return self.public(self.store.put_document(self.collection, ident, value, expected_version=0))

    def link_source_handoff(self, h, *, conversation_id=None):
        card = self.create({'provider': h['source'], 'conversation_id': conversation_id,
                            'access_profile_ref': h['access_profile_ref'], 'operation': h['operation']})
        row = self._read(card['id'])
        value = self._save(row, {'status': 'awaiting_user', 'job_id': h['job_id'],
                                'handoff_id': h['id'], 'handoff_href': h['href']})
        self.engine.handoffs.update(h['id'], {'connection_id': row['id'], 'conversation_id': conversation_id})
        return self.public(value)

    def _begin(self, row, action, key, expected_version):
        receipt = row.get('receipts', {}).get(key)
        if receipt:
            if receipt['action'] != action:
                raise DocumentConflict('Idempotency key is bound to another action')
            return row, True
        if row['state_version'] != expected_version:
            raise DocumentConflict('Connection changed; reload before acting')
        if row.get('inflight'):
            raise DocumentConflict('A connection action needs reconciliation before another action')
        return self._save(row, {'inflight': {'action': action, 'key': key}}), False

    def _finish(self, ident, action, key, changes):
        row = self._read(ident)
        if row.get('inflight') != {'action': action, 'key': key}:
            raise DocumentConflict('Connection action ownership changed')
        receipts = {**row.get('receipts', {}), key: {'action': action, 'completed_at': observed_at()}}
        return self._save(row, {**changes, 'inflight': None, 'receipts': receipts, 'observed_at': observed_at()})

    def _adapter(self, provider):
        adapter = self.engine.provider_auth.get(provider)
        if adapter is None:
            raise ValueError('Provider sign-in adapter is unavailable')
        return adapter

    async def _login_event(self, ident, event):
        if self._closed:
            return
        async with self.locks.setdefault(ident, asyncio.Lock()):
            row = self._read(ident)
            event_id = event.get('login_id') or event.get('loginId')
            if not event_id or event_id != row.get('login_id') or row['status'] != 'awaiting_auth':
                return
            status = event.get('status')
            if status not in {'ready', 'failed', 'cancelled', 'expired'}:
                return
            self.live_logins.pop(ident, None)
            self._save(row, {'status': status, 'observed_at': observed_at()})

    async def action(self, ident, body):
        action, key = body['action'], body['idempotency_key']
        if action not in {'start_login', 'cancel', 'refresh', 'mark_pending', 'open_provider', 'request_agent', 'verify'}:
            raise ValueError('Unknown connection action')
        async with self.locks.setdefault(ident, asyncio.Lock()):
            row, replay = self._begin(self._read(ident), action, key, body['expected_version'])
            if replay:
                return self.public(row)
            try:
                if row['kind'] == 'model':
                    changes = await self._model_action(row, action)
                else:
                    changes = await self._source_action(row, action, body)
                return self.public(self._finish(ident, action, key, changes))
            except BaseException as exc:
                with suppress(Exception):
                    self._finish(ident, action, key, {'status': 'failed', 'message_code': 'connection_action_failed'})
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, (DocumentConflict, AccessDenied)):
                    raise
                raise ValueError('Connection action could not complete; inspect its status and retry when available') from None

    async def _model_action(self, row, action):
        if row['provider'] in {'openai', 'anthropic'}:
            if action == 'cancel':
                return {'status': 'cancelled'}
            if action == 'refresh':
                return {'status': 'configured_unverified' if row.get('secret_refs', {}).get('api_key') else 'not_connected'}
            raise AccessDenied('API providers use the protected API-key field')
        adapter = self._adapter(row['provider'])
        if action == 'start_login':
            if row['status'] == 'cancel_unconfirmed':
                raise AccessDenied('The previous sign-in cancellation is not confirmed')
            if (await adapter.auth_status()).get('status') == 'ready':
                return {'status': 'ready', 'message_code': 'existing_account_connected'}
            callback = lambda event: self._login_event(row['id'], event)
            result = await adapter.start_login(on_event=callback)
            login_id = result.get('login_id') or result.get('loginId')
            public = {key: result[key] for key in ('verification_url', 'user_code', 'expires_at') if result.get(key) is not None}
            if public:
                self.live_logins[row['id']] = public
            return {'status': result.get('status', 'awaiting_auth'), 'login_id': login_id,
                    'message_code': result.get('message_code', 'complete_official_sign_in')}
        if action == 'cancel':
            self.live_logins.pop(row['id'], None)
            if row.get('login_id'):
                result = await adapter.cancel_login(row['login_id'])
                return {'status': result.get('status', 'cancelled') if result.get('acknowledged') else 'cancel_unconfirmed'}
            return {'status': 'cancelled'}
        if action == 'refresh':
            status = await adapter.auth_status()
            state = status.get('status', 'unknown')
            if state == 'ready':
                self.live_logins.pop(row['id'], None)
                return {'status': 'ready'}
            if row.get('login_id') and hasattr(adapter, 'login_status'):
                login = adapter.login_status(row['login_id'])
                if inspect.isawaitable(login):
                    login = await login
                if login.get('status') == 'awaiting_auth':
                    self.live_logins[row['id']] = {k: login[k] for k in ('verification_url', 'user_code', 'expires_at') if login.get(k) is not None}
                else:
                    self.live_logins.pop(row['id'], None)
                return {'status': login.get('status', state)}
            return {'status': state}
        raise ValueError('This action applies to a source provider')

    async def _source_action(self, row, action, body):
        if action == 'cancel':
            if row.get('workflow_run_id'):
                run = await self.engine.workflows.interrupt(row['workflow_run_id'])
                if run['status'] == 'interrupting' or any(item.get('status') == 'unconfirmed' for item in run.get('interruptions', [])):
                    return {'status': 'cancel_unconfirmed', 'message_code': 'agent_stop_not_confirmed'}
            if row.get('handoff_id'):
                self.engine.handoffs.update(row['handoff_id'], {'status': 'cancelled', 'resolution': 'connection_cancelled'})
            return {'status': 'cancelled'}
        if action == 'refresh':
            profile = self.engine.profile({'access_profile_ref': row['access_profile_ref']})
            return {'status': source_readiness(row['provider'], row['operation'], profile)['state']}
        if action == 'verify':
            if row['operation'] != 'search':
                raise AccessDenied('This connection needs a scoped browser or identifier check')
            from .onboarding import check_source
            profile = self.engine.profile({'access_profile_ref': row['access_profile_ref']})
            current = source_readiness(row['provider'], row['operation'], profile)
            if current['state'] == 'approval_pending' and not current.get('configured'):
                return {'status': 'approval_pending', 'message_code': 'pending_operation_excluded_from_admission'}
            result = await check_source(self.engine, self.browser, row['provider'],
                {'operation': row['operation'], 'access_profile_ref': row['access_profile_ref']})
            return {'status': result['state'], 'message_code': 'source_operation_checked'}
        if action == 'mark_pending':
            profile = self.engine.profile({'access_profile_ref': row['access_profile_ref']})
            profile.setdefault('source_readiness', {}).setdefault(row['provider'], {})[row['operation']] = {
                'state': 'approval_pending', 'observed_at': observed_at(), 'evidence_ref': 'connection:'+row['id'],
                'scope': 'operator_reported_provider_approval'}
            self.engine.save_profile(profile)
            if row.get('handoff_id'):
                self.engine.handoffs.update(row['handoff_id'], {'status': 'waiting_external'})
            return {'status': 'approval_pending', 'message_code': 'pending_operation_excluded_from_admission'}
        if action not in {'open_provider', 'request_agent'}:
            raise ValueError('This action applies to a model provider')
        if not row.get('handoff_id'):
            from .onboarding import setup_source
            h = await setup_source(self.engine, row['provider'], {'operation': row['operation'],
                'access_profile_ref': row['access_profile_ref'], 'conversation_id': row.get('conversation_id'),
                'connection_id': row['id']})
            row = self._save(row, {'job_id': h['job_id'], 'handoff_id': h['id'], 'handoff_href': h['href'],
                                   'access_profile_ref': h['access_profile_ref']})
        if action == 'open_provider':
            return {'status': 'awaiting_user'}
        if row.get('workflow_run_id'):
            run = self.engine.workflows.get_run(row['workflow_run_id'])
            job = self.engine.store.get_job(run['job_id'])
            if (job['mission'].get('operator_access', {}).get('purpose') != 'provider_setup'
                    or job['mission'].get('connection_id') != row['id'] or run['job_id'] != row.get('agent_job_id')
                    or self.engine.profile(job['mission'])['id'] != row['access_profile_ref']):
                raise AccessDenied('The existing workflow does not belong to this provider setup')
            enrollment = getattr(self.engine, 'provider_enrollment', None)
            pending = enrollment.list(row['id'])['actions'] if enrollment else []
            if any(item['status'] in {'pending', 'executing', 'uncertain'} for item in pending):
                return {'status': 'awaiting_user', 'message_code': 'review_provider_form_action_first'}
            if run['status'] in {'awaiting_user', 'paused'}:
                if any(item.get('status') == 'unconfirmed' for item in run.get('interruptions', [])):
                    return {'status': 'cancel_unconfirmed', 'message_code': 'agent_stop_not_confirmed'}
                if self.engine.handoffs.list(run['job_id'], 'active'):
                    return {'status': 'awaiting_user', 'message_code': 'complete_reviewed_provider_step'}
                await self.engine.workflows.resume(run['id'])
                return {'status': 'agent_requested', 'message_code': 'provider_setup_resumed'}
            if run['status'] == 'completed':
                return {'status': 'needs_verification', 'message_code': 'setup_complete_verify_connection'}
            if run['status'] not in {'queued', 'running', 'pending', 'waiting_children'}:
                return {'status': run['status'], 'message_code': 'provider_setup_needs_review'}
            return {'status': 'agent_requested'}
        backend = provider_name(body.get('agent_backend', 'codex'))
        if (await self._adapter(backend).auth_status()).get('status') != 'ready':
            return {'status': 'awaiting_model_auth', 'message_code': 'connect_model_before_optional_agent'}
        h = self.engine.handoffs.get(row['handoff_id'])
        from .connection_agent import provider_setup_plan
        model = body.get('agent_model')
        if backend == 'claude_code' and not model:
            return {'status': 'awaiting_model_auth', 'message_code': 'select_explicit_claude_model'}
        plan = provider_setup_plan(row, h, self.engine.store.get_job(row['job_id'])['mission'], backend, model)
        run = self.engine.workflows.create_run(plan)
        row = self._save(row, {'workflow_run_id': run['id'], 'agent_job_id': run['job_id']})
        await self.engine.workflows.start_run(run['id'])
        return {'status': 'agent_requested'}

    async def store_secret(self, ident, body):
        field, key = body['field'], body['idempotency_key']
        async with self.locks.setdefault(ident, asyncio.Lock()):
            row = self._read(ident)
            api_model = row['kind'] == 'model' and row['provider'] in {'openai', 'anthropic'}
            if row['kind'] != 'source' and not api_model:
                raise AccessDenied('Model login credentials belong to the official provider CLI')
            if api_model and field != 'api_key':
                raise AccessDenied('API model connections accept only an API key')
            from .connection_intent import credential_fields
            if field not in credential_fields(row['provider'], row['kind']):
                raise ValueError('Unsupported credential field for this provider')
            credential_id = body.get('credential_id')
            if credential_id and (api_model or field != 'api_key' or row['provider'] == 'pubmed'):
                raise ValueError('Named key pools are unavailable for this credential')
            profile = None
            pool = None
            if row['kind'] == 'source':
                profile = self.engine.profile({'access_profile_ref': row['access_profile_ref']})
                config = profile.get('sources', {}).get(row['provider'], {})
                if field == 'api_key' and (credential_id or config.get('credential_pool')):
                    pool = [dict(item) for item in config.get('credential_pool', [])]
                    if not pool and config.get('api_key_ref'):
                        pool = [{'id': 'primary', 'api_key_ref': config['api_key_ref']}]
                    credential_id = credential_id or ('primary' if any(item['id'] == 'primary' for item in pool) else pool[0]['id'])
                    if len(pool) >= 20 and not any(item['id'] == credential_id for item in pool):
                        raise ValueError('At most 20 saved keys are supported')
            action = 'secret:'+field + (':'+credential_id if credential_id else '')
            row, replay = self._begin(row, action, key, body['expected_version'])
            if replay:
                return self.public(row)
            # Stable private reference makes the vault write repeat-safe, but an
            # interrupted action is not automatically replayed after restart.
            ref = 'connection/'+ident+'/'+field + ('/'+credential_id if credential_id else '')
            try:
                self.engine.secrets.set(ref, body['value'])
                refs = {**row.get('secret_refs', {}), field: ref}
                changes = {'secret_refs': refs, 'message_code': 'credential_stored_operation_unverified'}
                if api_model:
                    changes['status'] = 'configured_unverified'
                else:
                    if profile['id'] == 'public':
                        profile = {**profile, 'id': 'connection-'+row['provider'], 'name': row['provider']+' connection'}
                    config = profile.setdefault('sources', {}).setdefault(row['provider'], {})
                    if pool is not None:
                        entry = next((item for item in pool if item['id'] == credential_id), None)
                        if entry is None:
                            pool.append({'id': credential_id, 'api_key_ref': ref, 'enabled': True})
                        else:
                            entry['api_key_ref'] = ref
                        config['credential_pool'] = pool
                        config['api_key_ref'] = next(item['api_key_ref'] for item in pool if item.get('enabled') is not False)
                    else:
                        config[field+'_ref'] = ref
                    readiness = profile.setdefault('source_readiness', {}).setdefault(row['provider'], {})
                    for operation in ('search', 'resolve'):
                        if operation in readiness:
                            readiness[operation] = {'state': 'unknown', 'scope': 'credentials_changed'}
                    self.engine.save_profile(profile)
                    changes['access_profile_ref'] = profile['id']
                    changes['status'] = 'needs_verification'
                # Storage never establishes provider entitlement or quota.
                return self.public(self._finish(ident, action, key, changes))
            except Exception:
                with suppress(Exception):
                    self._finish(ident, action, key, {'status': 'needs_review', 'message_code': 'credential_write_needs_review'})
                raise ValueError('Credential storage could not complete; inspect the connection before retrying') from None

    async def submit_login_code(self, ident, body):
        async with self.locks.setdefault(ident, asyncio.Lock()):
            row = self._read(ident)
            if row['kind'] != 'model' or row['provider'] != 'claude_code' or not row.get('login_id'):
                raise AccessDenied('This connection does not accept an official login completion code')
            key = body['idempotency_key']
            row, replay = self._begin(row, 'login_code', key, body['expected_version'])
            if replay:
                return self.public(row)
            try:
                await self._adapter(row['provider']).submit_login(row['login_id'], body['code'])
                result = self._finish(ident, 'login_code', key, {'status': 'awaiting_auth'})
                return self.public(result)
            except Exception:
                self._finish(ident, 'login_code', key, {'status': 'failed', 'message_code': 'login_code_not_accepted'})
                raise ValueError('Official sign-in code could not be submitted') from None

    async def refresh_pending(self, ident=None):
        """Poll owned CLI process state, without starting login or model work."""
        rows = [self._read(ident)] if ident else self.store.list_documents(self.collection)
        for value in rows:
            if value['kind'] != 'model' or not value.get('login_id') or value['status'] != 'awaiting_auth':
                continue
            adapter = self.engine.provider_auth.get(value['provider'])
            if not adapter or not hasattr(adapter, 'login_status'):
                continue
            async with self.locks.setdefault(value['id'], asyncio.Lock()):
                row = self._read(value['id'])
                if row['status'] != 'awaiting_auth' or row.get('inflight'):
                    continue
                login = adapter.login_status(row['login_id'])
                if inspect.isawaitable(login):
                    login = await login
                status = login.get('status', 'unknown')
                if status == 'awaiting_auth':
                    self.live_logins[row['id']] = {k: login[k] for k in ('verification_url', 'user_code', 'expires_at') if login.get(k) is not None}
                elif status in {'ready', 'failed', 'cancelled', 'expired', 'interrupted'}:
                    self.live_logins.pop(row['id'], None)
                    self._save(row, {'status': status, 'observed_at': observed_at()})

    async def providers_status(self, include_usage=False):
        async def one(provider, adapter):
            try:
                auth = await adapter.auth_status()
                quota = await adapter.quota_status() if auth.get('status') == 'ready' else unknown_quota()
                auth = {k: auth[k] for k in ('status', 'installed', 'mode', 'plan_type', 'observed_at', 'message_code') if k in auth}
                quota = {**unknown_quota(), **{k: quota[k] for k in ('status', 'ordinary_usage_allowed', 'observed_at', 'stale') if k in quota},
                         'windows': [{k: w[k] for k in ('id', 'used_percent', 'remaining_percent', 'window_minutes', 'resets_at') if k in w}
                                     for w in quota.get('windows', []) if isinstance(w, dict)]}
                value = {'provider': provider, 'auth': auth, 'quota': quota}
                if include_usage and hasattr(adapter, 'usage_status'):
                    usage = await adapter.usage_status()
                    value['usage'] = {k: usage[k] for k in ('status', 'observed_at', 'scope') if k in usage}
                    value['usage']['summary'] = {k: usage['summary'][k] for k in ('lifetimeTokens', 'peakDailyTokens', 'currentStreakDays')
                        if isinstance(usage.get('summary'), dict) and type(usage['summary'].get(k)) is int and usage['summary'][k] >= 0}
                return value
            except Exception:
                return {'provider': provider, 'auth': {'status': 'unknown'}, 'quota': unknown_quota('unsupported')}
        from .credentials import CredentialPool
        source_quotas = []
        for profile in self.engine.profiles():
            try:
                source_quotas.extend({**value, 'access_profile_ref': profile['id']}
                    for value in CredentialPool(self.store, profile, self.engine.secrets.get).public_status())
            except ValueError:
                source_quotas.append({'access_profile_ref': profile['id'], 'status': 'invalid_configuration',
                                      'scope': 'provider_quota_group'})
        providers = list(await asyncio.gather(*(one(name, adapter) for name, adapter in self.engine.provider_auth.items())))
        for provider in ('openai', 'anthropic'):
            configured = any(r['provider'] == provider and r['status'] != 'cancelled' and r.get('secret_refs', {}).get('api_key')
                             for r in self.store.list_documents(self.collection))
            providers.append({'provider': provider, 'auth': {'status': 'configured_unverified' if configured else 'not_configured',
                'mode': 'api_key', 'observed_at': observed_at()}, 'quota': unknown_quota('unsupported')})
        return {'providers': providers,
                'source_quotas': source_quotas, 'observed_at': observed_at(),
                'scope': 'provider_account_not_request_budget'}

    async def close(self):
        self._closed = True
        self.live_logins.clear()
        for adapter in self.engine.provider_auth.values():
            if hasattr(adapter, 'close'):
                with suppress(Exception):
                    await adapter.close()


def create_connection_router(engine, browser):
    manager = getattr(engine, 'connections', None) or ConnectionManager(engine, browser)
    engine.connections = manager
    router = APIRouter(tags=['connections'])

    @router.get('/v1/connections')
    async def cards(conversation_id: str | None = None):
        manager._conversation(conversation_id)
        await manager.refresh_pending()
        return manager.list(conversation_id)

    @router.post('/v1/connections', status_code=201)
    async def create(body: ConnectionInput):
        return manager.create(body.model_dump())

    @router.get('/v1/connections/{ident}')
    async def get(ident: str):
        await manager.refresh_pending(ident)
        return manager.public(manager._read(ident))

    @router.post('/v1/connections/{ident}/actions')
    async def action(ident: str, body: ConnectionAction):
        return await manager.action(ident, body.model_dump())

    @router.post('/v1/connections/{ident}/secret')
    async def secret(ident: str, body: ConnectionSecret):
        return await manager.store_secret(ident, body.model_dump())

    @router.post('/v1/connections/{ident}/login-code')
    async def login_code(ident: str, body: LoginCode):
        return await manager.submit_login_code(ident, body.model_dump())

    @router.get('/v1/providers/status')
    async def status(include_usage: bool = False):
        return await manager.providers_status(include_usage)

    return router
