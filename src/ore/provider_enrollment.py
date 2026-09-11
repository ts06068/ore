"""Provider setup forms with host-only values and reviewable, single-use effects."""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field

from .models import canonical_digest
from .policy import AccessDenied
from .provider_auth import observed_at
from .store import DocumentConflict

DETAIL_FIELDS = {'given_name', 'family_name', 'affiliation', 'application_name', 'website'}
SECRET_FIELDS = {'username', 'password', 'mfa_code'}
SELECTOR = 'a,button,input,textarea,select,[role="button"]'
SEMANTICS = [
    ('mfa_code', r'one.?time|verification.?code|mfa|otp'),
    ('password', r'password|passwd'), ('username', r'e.?mail|username|user.?name|login.?id'),
    ('given_name', r'given.?name|first.?name'), ('family_name', r'family.?name|last.?name|surname'),
    ('affiliation', r'affiliation|institution|organization'),
    ('application_name', r'application.?name|app.?name|label'), ('website', r'website|site.?url'),
    ('api_key', r'api.?key|access.?key|developer.?key'),
    ('terms', r'terms|agree|consent'), ('register', r'register|sign.?up|create.?account'),
    ('submit', r'submit|continue|next|save|sign.?in|log.?in|generate|request|create'),
]

# Values, page bodies, arbitrary URLs and raw text are intentionally absent.
FORM_SCRIPT = """els => els.map((e,index) => {
 const f=e.form||e.closest('form'); const r=e.getBoundingClientRect();
 return {index,tag:e.tagName.toLowerCase(),type:(e.getAttribute('type')||'').toLowerCase(),
 visible:!!(r.width&&r.height),disabled:!!e.disabled,readonly:!!e.readOnly,
 name:e.getAttribute('name')||'',id:e.id||'',autocomplete:e.getAttribute('autocomplete')||'',
 label:((e.labels&&Array.from(e.labels).map(x=>x.innerText).join(' '))||e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.innerText||'').slice(0,500),
 checked:!!e.checked,value_signature:('value' in e)?e.value:null,form:f?Array.from(document.forms).indexOf(f):null,
 action:f?f.action:null,method:f?(f.method||'get'):null,href:e.tagName==='A'?e.href:null};
}).slice(0,512)"""


def browser_authority(profile):
    """Keep browser/network authority fixed while protected API refs are added."""
    value = deepcopy(profile)
    for source, config in list(value.get('sources', {}).items()):
        if isinstance(config, dict):
            for key in ('username_ref', 'password_ref', 'api_key_ref', 'inst_token_ref', 'institution_token_ref', 'credential_pool'):
                config.pop(key, None)
            if not config:
                value['sources'].pop(source)
    if value.get('sources') == {}:
        value.pop('sources')
    # Search/resolve verification is not browser authority; browser readiness stays bound.
    for source, readiness in list(value.get('source_readiness', {}).items()):
        if isinstance(readiness, dict):
            readiness.pop('search', None)
            readiness.pop('resolve', None)
            if not readiness:
                value['source_readiness'].pop(source)
    if value.get('source_readiness') == {}:
        value.pop('source_readiness')
    return value


def is_setup(session):
    return (getattr(session, 'mission', {}).get('operator_access') or {}).get('purpose') == 'provider_setup'


def semantic(raw):
    words = ' '.join(str(raw.get(k, '')) for k in ('name', 'id', 'label', 'autocomplete', 'type')).casefold()
    for name, pattern in SEMANTICS:
        if re.search(pattern, words):
            return name
    return None


def redact_setup(session, value, secrets):
    """Mask known protected values from all later setup telemetry, not just inputs."""
    refs = getattr(session, 'enrollment_secret_refs', set())
    hidden = [secrets.get(ref) for ref in refs] + list(getattr(session, 'enrollment_ephemeral_values', []))
    hidden = sorted((v for v in hidden if isinstance(v, str) and v), key=len, reverse=True)
    def clean(item):
        if isinstance(item, str):
            if item.startswith(('https://', 'http://')):
                item = safe_setup_url(item)
            for text in hidden:
                item = item.replace(text, '[protected]')
            return item
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        return item
    return clean(value)


def safe_setup_url(url):
    """Setup links never disclose query tokens or long opaque path components."""
    value = urlsplit(url)
    path = '/'.join('[protected]' if len(part) > 24 and re.fullmatch(r'[A-Za-z0-9_.~-]+', part) else part
                    for part in value.path.split('/'))
    return urlunsplit((value.scheme, value.netloc, path, '', ''))


async def form_snapshot(session, secrets):
    if getattr(session.context, 'desktop', False) or getattr(session.context, 'companion', False):
        raise AccessDenied('Approved form actions need a local DOM-capable browser; use the existing desktop handoff for this transport')
    raw = await session.page.locator(SELECTOR).evaluate_all(FORM_SCRIPT)
    elements = []
    for item in raw:
        if item.get('visible') is False:
            continue
        hint = semantic(item)
        elements.append({'index': item['index'], 'tag': item['tag'], 'type': item.get('type', ''),
                         'label': (hint or item['tag']).replace('_', ' '), 'field_hint': hint,
                         'disabled': bool(item.get('disabled')), 'readonly': bool(item.get('readonly')),
                         'form': item.get('form'), 'checked': bool(item.get('checked')),
                         **({'href': safe_setup_url(item['href'])} if item.get('href') else {})})
    raw_url = session.page.url
    # Hash raw form identity locally so changed labels/action/URL invalidate consent.
    fingerprint = canonical_digest({'url': raw_url, 'page_identity': id(session.page), 'elements': raw})
    structure = canonical_digest({'url': raw_url, 'page_identity': id(session.page),
        'elements': [{key: value for key, value in item.items() if key != 'value_signature'} for item in raw]})
    return {'url': redact_setup(session, safe_setup_url(raw_url), secrets), 'url_digest': canonical_digest(raw_url),
            'form_fingerprint': fingerprint, 'structure_fingerprint': structure, 'elements': elements}, raw


async def block_setup_download(manager, session, item):
    """Never stage a provider-issued credential file as ordinary corpus evidence."""
    if not is_setup(session):
        return False
    code = 'provider_setup_use_protected_key_capture'
    try:
        await item.cancel()
    except (PlaywrightError, RuntimeError, TimeoutError):
        code = 'provider_setup_download_cancel_unconfirmed'
    await manager._emit(session.job_id, 'browser.download_blocked', {'session_id': session.id, 'code': code})
    return True


async def setup_browser_observation(manager, session):
    """Public setup observation excludes body text, values, screenshots and titles."""
    base = await manager.summary(session)
    if getattr(session.context, 'desktop', False) or getattr(session.context, 'companion', False):
        return {**base, 'title': 'Provider setup', 'text': '', 'elements': [], 'image_url': None,
                'form_actions_supported': False, 'screenshot_omitted': 'protected_provider_setup',
                'message': 'Use the protected connection controls or the operator desktop for this browser transport.'}
    snapshot, _ = await form_snapshot(session, manager.secrets)
    session.elements = snapshot['elements']
    return {**base, 'title': 'Provider setup', 'url': snapshot['url'], 'text': '',
            'elements': snapshot['elements'], 'form_fingerprint': snapshot['form_fingerprint'],
            'image_url': None, 'screenshot_omitted': 'protected_provider_setup', 'form_actions_supported': True,
            'pending_form_action_id': getattr(session, 'enrollment_pending', None)}


class EnrollmentDetails(BaseModel):
    model_config = ConfigDict(extra='forbid')
    values: dict[str, str]


class EnrollmentSecret(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str
    value: str = Field(min_length=1, max_length=256, repr=False)


class FormApproval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9._:-]{4,120}$')


class ProviderEnrollment:
    collection = 'connection.form_action'

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.locks = {}
        for row in self.store.list_documents(self.collection):
            if row.get('status') == 'executing':
                self._save(row, {'status': 'uncertain', 'message_code': 'interrupted_effect_do_not_replay'})

    def _read(self, ident, connection_id):
        row = self.store.get_document(self.collection, ident)
        if not row or row.get('connection_id') != connection_id:
            raise KeyError('Unknown form action')
        return row

    def _save(self, row, changes):
        return self.store.put_document(self.collection, row['id'], {**row, **changes}, expected_version=row['state_version'])

    def _connection(self, ident):
        row = self.engine.connections._read(ident)
        if row['kind'] != 'source' or row['status'] in {'cancelled', 'cancelling', 'cancel_unconfirmed'}:
            raise AccessDenied('An active source connection is required')
        return row

    def details(self, ident):
        self._connection(ident)
        row = self.store.get_document('connection.enrollment_details', ident) or {}
        values = row.get('values', {})
        code = self.store.get_document('connection.enrollment_code', ident) or {}
        protected = ['mfa_code'] if code.get('expires_at', 0) > time.time() and not code.get('consumed') else []
        return {'values': values, 'configured_fields': sorted(values), 'configured_protected_fields': protected}

    def save_details(self, ident, values):
        self._connection(ident)
        if not values.keys() <= DETAIL_FIELDS or any(not isinstance(v, str) or len(v) > 1000 for v in values.values()):
            raise ValueError('Provide approved name, affiliation, application name or website fields only')
        prior = self.details(ident)['values']
        self.store.put_document('connection.enrollment_details', ident,
                                {'id': ident, 'values': {key: value for key, value in {**prior, **values}.items() if value.strip()}, 'actor': 'operator'})
        return self.details(ident)

    def store_code(self, ident, field, value):
        self._connection(ident)
        if field != 'mfa_code':
            raise ValueError('Only a one-time verification code is accepted here')
        ref = 'connection-enrollment-code:' + ident
        self.engine.secrets.set(ref, value)
        self.store.put_document('connection.enrollment_code', ident,
            {'id': ident, 'ref': ref, 'expires_at': time.time() + 300, 'consumed': False})
        return {'stored': True, 'field': field, 'expires_in_seconds': 300}

    @staticmethod
    def public(row):
        keys = {'id', 'connection_id', 'state_version', 'status', 'kind', 'url', 'form_fingerprint',
                'session_id', 'epoch', 'fields', 'target', 'created_at', 'message_code', 'completed_at', 'handoff_id', 'handoff_href'}
        return {key: row[key] for key in keys if key in row}

    def list(self, ident):
        self._connection(ident)
        return {'actions': [self.public(row) for row in self.store.list_documents(self.collection)
                            if row['connection_id'] == ident][-40:]}

    async def rebind(self, runtime, session):
        """Resume the same setup node's browser after its old fenced task settled."""
        if not runtime.task or session.agent_id == runtime.actor_id or not is_setup(session):
            return
        async with session.lock:
            if session.agent_id == runtime.actor_id:
                return
            if session.control != 'agent' or getattr(session, 'enrollment_pending', None):
                raise AccessDenied('The setup browser is held by an operator or pending review')
            old_id = session.agent_id.removeprefix('task:')
            old = self.store.get_task(old_id)
            new = self.store.get_task(runtime.task['id'])
            if (not old or not new or session.job_id != runtime.job_id
                    or old['job_id'] != new['job_id'] or old['job_id'] != session.job_id
                    or old.get('state') not in {'awaiting_user', 'awaiting_auth', 'paused'}
                    or old.get('revision') != new.get('revision')
                    or old.get('input', {}).get('run_id') != new.get('input', {}).get('run_id')
                    or old.get('input', {}).get('node_id') != new.get('input', {}).get('node_id')):
                raise AccessDenied('The browser cannot transfer to a different or active task')
            self.store.validate_task_lease(new['id'], runtime.task['worker_id'], runtime.task['fence'], runtime.task['revision'])
            node = next((node for node in self.engine.workflows.nodes(new['input']['run_id'])
                         if node['id'] == new['input']['node_id']), None)
            if not node or node.get('task_id') != new['id']:
                raise AccessDenied('Only the current setup node attempt may reclaim its browser')
            released = self.store.release_control(session.job_id, session.id, session.agent_id, session.epoch)
            acquired = self.store.acquire_control(session.job_id, session.id, runtime.actor_id, 'agent', released['epoch'], 3600)
            session.agent_id, session.epoch = runtime.actor_id, acquired['epoch']

    async def _session(self, connection_id, session_id, epoch, *, runtime=None):
        row = self._connection(connection_id)
        if getattr(getattr(self.engine, 'execution', None), 'enabled', False):
            raise AccessDenied('Approved form actions require the local coordinator browser')
        session = self.engine.browser.get(session_id)
        if session.closed or session.closing or not is_setup(session) or session.epoch != epoch:
            raise DocumentConflict('Provider setup session changed; inspect the form again')
        job = self.store.get_job(session.job_id)
        if not job or job['mission'].get('connection_id') != connection_id or row.get('agent_job_id') != session.job_id:
            raise AccessDenied('Browser does not belong to this provider setup connection')
        if job.get('status') in {'cancelled', 'superseded'}:
            raise AccessDenied('Provider setup was cancelled or superseded')
        if runtime and (runtime.job_id != session.job_id or runtime.actor_id and runtime.actor_id != session.agent_id):
            raise AccessDenied('Provider form tool cannot access another task browser')
        profile = self.engine.profile(job['mission'])
        if browser_authority(profile) != browser_authority(session.policy.profile) or profile.get('id', 'public') != row['access_profile_ref']:
            raise DocumentConflict('Provider setup access profile changed')
        await self.engine.browser._authorize(session, session.control, epoch)
        await session.policy.check(session.page.url)
        return row, session, job, profile

    def _value(self, row, field):
        if field in SECRET_FIELDS:
            if field == 'mfa_code':
                code = self.store.get_document('connection.enrollment_code', row['id']) or {}
                if code.get('consumed') or code.get('expires_at', 0) <= time.time():
                    raise AccessDenied('Provide a fresh one-time verification code in this connection card')
                ref = code['ref']
            else:
                ref = row.get('secret_refs', {}).get(field)
            value = self.engine.secrets.get(ref) if ref else None
            if not value:
                raise AccessDenied('Store the requested field through the protected connection controls first')
            return value, ref
        if field not in DETAIL_FIELDS:
            raise AccessDenied('The agent may use only approved enrollment field names')
        value = self.details(row['id'])['values'].get(field)
        if not value:
            raise AccessDenied('Provide the requested enrollment detail in this connection card first')
        return value, None

    def _fill_fields(self, row, snapshot, fields, *, automatic):
        if not isinstance(fields, list) or not 1 <= len(fields) <= 16:
            raise ValueError('A fill requires 1 to 16 field bindings')
        available = {item['index']: item for item in snapshot['elements']}
        output, seen = [], set()
        for binding in fields:
            if set(binding) != {'target', 'value_ref'} or type(binding['target']) is not int:
                raise ValueError('Field bindings contain only target and value_ref')
            item = available.get(binding['target']); name = binding['value_ref']
            if not item or item['index'] in seen or item['tag'] not in {'input', 'textarea'} or item['disabled'] or item['readonly']:
                raise AccessDenied('An enabled, observed input field is required')
            if item['tag'] == 'input' and item['type'] not in {'', 'text', 'email', 'password', 'tel', 'url'} and not (name == 'mfa_code' and item['type'] == 'number'):
                raise AccessDenied('Unsupported preparatory input type')
            if name == 'password' and item['type'] != 'password' or item['type'] == 'password' and name not in {'password', 'mfa_code'}:
                raise AccessDenied('Password references may only fill password inputs')
            if automatic and item['field_hint'] != name:
                raise AccessDenied('This field binding is ambiguous; propose_fill for explicit review')
            self._value(row, name)
            output.append({'target': item['index'], 'label': item['label'], 'value_ref': name, 'protected': name in SECRET_FIELDS})
            seen.add(item['index'])
        return output

    async def tool(self, runtime, args):
        job = runtime.job(); ident = job['mission'].get('connection_id')
        if not ident or job['mission'].get('operator_access', {}).get('purpose') != 'provider_setup':
            raise AccessDenied('Provider form tools are limited to provider setup workflows')
        session = self.engine.browser.get(args['session_id'])
        await self.rebind(runtime, session)
        row, session, job, profile = await self._session(ident, args['session_id'], args['epoch'], runtime=runtime)
        async with session.lock:
            row, session, job, profile = await self._session(ident, args['session_id'], args['epoch'], runtime=runtime)
            snapshot, _ = await form_snapshot(session, self.engine.secrets)
            op = args['operation']
            if op == 'inspect':
                return {**snapshot, 'connection_id': ident, 'session_id': session.id, 'epoch': session.epoch,
                        'available_value_refs': sorted(set(row.get('secret_refs', {})) & SECRET_FIELDS | set(self.details(ident)['values']) | set(self.details(ident)['configured_protected_fields'])),
                        'actions': self.list(ident)['actions']}
            if getattr(session, 'enrollment_pending', None):
                raise DocumentConflict('Review the pending form action before changing this browser')
            if args.get('form_fingerprint') != snapshot['form_fingerprint'] or args.get('url') != snapshot['url']:
                raise DocumentConflict('The form or URL changed; inspect it again before acting')
            fields = self._fill_fields(row, snapshot, args.get('fields'), automatic=op == 'fill') if op in {'fill', 'propose_fill'} else None
            target = None
            if op in {'propose_click', 'capture_key'}:
                target = next((e for e in snapshot['elements'] if e['index'] == args.get('target')), None)
                if not target or target['disabled']:
                    raise AccessDenied('Choose an enabled target in the inspected form')
                if op == 'propose_click' and (target['form'] is None or target['tag'] not in {'button', 'input'}
                        or target['tag'] == 'input' and target['type'] not in {'submit', 'button', 'checkbox', 'radio'}):
                    raise AccessDenied('Reviewed clicks are limited to observed form controls')
                if op == 'capture_key' and (target['tag'] not in {'input', 'textarea'} or target['type'] == 'password'):
                    raise AccessDenied('Key capture requires an observed API-key input or textarea')
            if op not in {'fill', 'propose_fill', 'propose_click', 'capture_key'}:
                raise ValueError('Unknown provider form operation')
            action_id = str(uuid.uuid4())
            action = {'id': action_id, 'connection_id': ident, 'session_id': session.id, 'epoch': session.epoch,
                      'status': 'pending', 'kind': 'fill' if fields else 'click' if op == 'propose_click' else 'capture_key',
                      'url': snapshot['url'], 'url_digest': snapshot['url_digest'], 'form_fingerprint': snapshot['form_fingerprint'],
                      'structure_fingerprint': snapshot['structure_fingerprint'],
                      'profile_digest': canonical_digest(browser_authority(profile)), 'mission_digest': canonical_digest(job['mission']),
                      'job_id_ref': job['id'], 'job_revision': job['revision'], 'job_generation': job['generation'],
                      'fields': fields or [], 'target': target, 'created_at': observed_at(),
                      'values_digest': canonical_digest([self._value(row, f['value_ref'])[0] for f in fields]) if fields else None}
            action = self.store.put_document(self.collection, action_id, action, expected_version=0)
            session.enrollment_pending = action_id
            if op == 'fill':
                return await self._perform(row, session, action, key='automatic-' + action_id)
            # Do not reuse or resolve an unrelated authentication/challenge handoff.
            handoff = self.engine.handoffs.create(job['id'], 'source_setup',
                'Review the exact provider form action in its connection card.',
                task_id=runtime.task.get('id') if runtime.task else None,
                context={'url': session.page.url, 'epoch': session.epoch, 'episode': action_id})
            handoff = self.engine.handoffs.update(handoff['id'], {'session_id': session.id, 'session_state': 'live',
                'source': row['provider'], 'operation': row['operation'], 'form_action_id': action_id})
            action = self._save(action, {'handoff_id': handoff['id'], 'handoff_href': handoff['href']})
            return {**self.public(action), 'awaiting_approval': True, 'needs_user': True,
                    'handoff_url': handoff['href'],
                    'message': 'Review this exact form action in the connection card. Credentials remain protected.'}

    async def _perform(self, row, session, action, *, key):
        # The receipt is durable before any input. A crash or timeout cannot replay a submit.
        action = self._save(action, {'status': 'executing', 'approval_key': key})
        try:
            if action['kind'] == 'fill':
                for field in action['fields']:
                    value, ref = self._value(row, field['value_ref'])
                    if ref:
                        session.enrollment_secret_refs = set(getattr(session, 'enrollment_secret_refs', set())) | {ref}
                    fresh, _ = await form_snapshot(session, self.engine.secrets)
                    if fresh['structure_fingerprint'] != action['structure_fingerprint']:
                        raise DocumentConflict('The form changed during preparation')
                    if field['value_ref'] == 'mfa_code':
                        session.enrollment_ephemeral_values = [*getattr(session, 'enrollment_ephemeral_values', []), value]
                        code = self.store.get_document('connection.enrollment_code', row['id'])
                        self.store.put_document('connection.enrollment_code', row['id'], {**code, 'consumed': True})
                        self.engine.secrets.set(ref, '')
                    await session.page.locator(SELECTOR).nth(field['target']).fill(value, timeout=15000)
            elif action['kind'] == 'capture_key':
                value = await session.page.locator(SELECTOR).nth(action['target']['index']).input_value(timeout=15000)
                if not value or len(value) > 65536:
                    raise AccessDenied('The selected field does not contain an API key')
                current = self.engine.connections._read(row['id'])
                await self.engine.connections.store_secret(row['id'], {'field': 'api_key', 'value': value,
                    'expected_version': current['state_version'], 'idempotency_key': 'capture-' + action['id']})
                current = self.engine.connections._read(row['id'])
                ref = current.get('secret_refs', {}).get('api_key')
                if ref:
                    session.enrollment_secret_refs = set(getattr(session, 'enrollment_secret_refs', set())) | {ref}
            else:
                await session.page.locator(SELECTOR).nth(action['target']['index']).click(timeout=15000)
            action = self._save(action, {'status': 'completed', 'completed_at': observed_at(), 'message_code': 'observed_form_action_completed'})
            session.enrollment_pending = None
        except (Exception, asyncio.CancelledError) as exc:
            self._save(action, {'status': 'uncertain', 'message_code': 'effect_outcome_unknown_inspect_before_retry'})
            if isinstance(exc, asyncio.CancelledError):
                raise
            # Do not relay Playwright errors, which can include entered field values.
            raise DocumentConflict('Form action outcome is uncertain; inspect the browser before taking another action') from None
        return self.public(action)

    async def decide(self, ident, action_id, body, *, approve):
        async with self.locks.setdefault(action_id, asyncio.Lock()):
            action = self._read(action_id, ident)
            if action.get('approval_key') == body['idempotency_key']:
                return self.public(action)
            if action['status'] != 'pending' or action['state_version'] != body['expected_version']:
                raise DocumentConflict('Form action is no longer pending; do not replay it')
            self._connection(ident)
            if not approve:
                result = self._save(action, {'status': 'rejected', 'approval_key': body['idempotency_key']})
                session = getattr(self.engine.browser, 'sessions', {}).get(action['session_id'])
                if session and getattr(session, 'enrollment_pending', None) == action_id:
                    session.enrollment_pending = None
                if action.get('handoff_id'):
                    self.engine.handoffs.update(action['handoff_id'], {'status': 'resolved', 'resolution': 'form_action_declined'})
                return self.public(result)
            try:
                row, session, job, profile = await self._session(ident, action['session_id'], action['epoch'])
            except (AccessDenied, DocumentConflict, KeyError):
                self._save(action, {'status': 'stale', 'message_code': 'form_session_changed_prepare_again'})
                session = getattr(self.engine.browser, 'sessions', {}).get(action['session_id'])
                if session and getattr(session, 'enrollment_pending', None) == action_id:
                    session.enrollment_pending = None
                raise
            async with session.lock:
                row, session, job, profile = await self._session(ident, action['session_id'], action['epoch'])
                snapshot, _ = await form_snapshot(session, self.engine.secrets)
                current = (snapshot['url_digest'], snapshot['form_fingerprint'], canonical_digest(browser_authority(profile)), canonical_digest(job['mission']), job['revision'], job['generation'])
                expected = (action['url_digest'], action['form_fingerprint'], action['profile_digest'], action['mission_digest'], action['job_revision'], action['job_generation'])
                if current != expected or session.enrollment_pending != action_id:
                    self._save(action, {'status': 'stale', 'message_code': 'form_changed_inspect_before_reapproval'})
                    session.enrollment_pending = None
                    raise DocumentConflict('The reviewed form changed; inspect and propose the current action again')
                if action['fields'] and canonical_digest([self._value(row, f['value_ref'])[0] for f in action['fields']]) != action['values_digest']:
                    raise DocumentConflict('Approved enrollment values changed; prepare a new form action')
                result = await self._perform(row, session, action, key=body['idempotency_key'])
            if action.get('handoff_id'):
                self.engine.handoffs.update(action['handoff_id'], {'status': 'resolved', 'resolution': 'form_action_approved'})
            # Continue only the same source-setup workflow, never a collection mission.
            run_id = row.get('workflow_run_id')
            if run_id:
                run = self.engine.workflows.get_run(run_id)
                if run['status'] in {'awaiting_user', 'paused'} and run.get('job_id') == session.job_id:
                    await self.engine.workflows.resume(run_id)
            return result


def create_provider_enrollment_router(engine):
    manager = getattr(engine, 'provider_enrollment', None) or ProviderEnrollment(engine)
    engine.provider_enrollment = manager
    router = APIRouter(tags=['connections'])

    @router.get('/v1/connections/{ident}/enrollment/details')
    async def details(ident: str):
        return manager.details(ident)

    @router.post('/v1/connections/{ident}/enrollment/details')
    async def save_details(ident: str, body: EnrollmentDetails):
        return manager.save_details(ident, body.values)

    @router.post('/v1/connections/{ident}/enrollment/secret')
    async def secret(ident: str, body: EnrollmentSecret):
        return manager.store_code(ident, body.field, body.value)

    @router.get('/v1/connections/{ident}/form-actions')
    async def actions(ident: str):
        return manager.list(ident)

    @router.post('/v1/connections/{ident}/form-actions/{action_id}/approve')
    async def approve(ident: str, action_id: str, body: FormApproval):
        return await manager.decide(ident, action_id, body.model_dump(), approve=True)

    @router.post('/v1/connections/{ident}/form-actions/{action_id}/reject')
    async def reject(ident: str, action_id: str, body: FormApproval):
        return await manager.decide(ident, action_id, body.model_dump(), approve=False)

    return router
