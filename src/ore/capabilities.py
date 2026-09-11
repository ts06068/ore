"""Versioned capabilities shared by planning, workflows, and installed plugins.

The legacy actions remain adapters. A new domain does not need to change a core
intent enum or the coordinator's dispatch table. Generated programs use the same
contract and run through the isolated code capability, never coordinator imports.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
from importlib.metadata import entry_points
import inspect
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

from .models import canonical_digest
from .policy import AccessDenied, redact

OBJECT = {'type': 'object'}
STRING = {'type': 'string'}


def object_schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False}


def validate_schema(schema):
    Draft202012Validator.check_schema(schema)
    def refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('$ref', '$dynamicRef') and not str(child).startswith('#'):
                    raise ValueError('Capability schemas may only contain local references')
                refs(child)
        elif isinstance(value, list):
            for child in value: refs(child)
    refs(schema)


def validate_value(value, schema):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: str(list(error.path)))
    if errors:
        error = errors[0]
        # Do not echo arbitrary instance values (which may include signed URLs).
        raise ValueError(f'Capability contract failed at {list(error.path)}: {error.validator}')


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    output_schema: dict = field(default_factory=lambda: dict(OBJECT))
    version: str = '1'
    read_only: bool = False
    execution: str = 'coordinator'
    permissions: tuple[str, ...] = ()
    timeout_seconds: float = 180
    replay_safe: bool = False
    handler_digest: str | None = None

    def public(self):
        value = {key: getattr(self, key) for key in self.__dataclass_fields__}
        value['permissions'] = list(self.permissions)
        value['digest'] = canonical_digest(value)
        return value


class CapabilityRegistry:
    def __init__(self, engine, *, plugins=True):
        self.engine = engine
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Callable] = {}
        self.plugin_errors: list[dict] = []
        self._builtins()
        if plugins:
            for point in entry_points(group='ore.capabilities'):
                try:
                    point.load()(self)
                except Exception as exc:
                    self.plugin_errors.append({'plugin': point.name, 'error': type(exc).__name__})

    def register(self, spec: ToolSpec, handler: Callable):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,159}', spec.name):
            raise ValueError('Invalid capability name')
        if spec.name in self._specs:
            raise ValueError('Capability names are immutable within a registry')
        if spec.timeout_seconds <= 0 or not callable(handler):
            raise ValueError('Capability requires a positive timeout and callable handler')
        validate_schema(spec.input_schema); validate_schema(spec.output_schema)
        if not inspect.iscoroutinefunction(handler):
            raise ValueError('Capability handlers must be async and settle cancellation; use code.run for blocking generated programs')
        if spec.handler_digest is None:
            source = inspect.getsourcefile(handler)
            try: implementation = Path(source).read_bytes() if source else inspect.getsource(handler).encode()
            except (OSError, TypeError): implementation = repr(type(handler)).encode()
            spec = replace(spec, handler_digest=hashlib.sha256(implementation).hexdigest())
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def spec(self, name):
        if name not in self._specs: raise AccessDenied('Unknown capability')
        return self._specs[name]

    def catalog(self):
        return [self._specs[name].public() for name in sorted(self._specs)]

    async def execute(self, name, arguments, runtime):
        spec = self.spec(name)
        if not isinstance(arguments, dict): raise ValueError('Capability arguments must be an object')
        validate_value(arguments, spec.input_schema)
        job = runtime.job()
        if job['status'] in ('paused', 'cancelled', 'awaiting_user', 'awaiting_auth', 'interrupting'):
            raise AccessDenied('Execution is paused')
        if getattr(runtime, 'read_only', False) and not spec.read_only:
            raise AccessDenied('This capability requires an approved execution plan')
        if runtime.task:
            self.engine.store.validate_task_lease(runtime.task['id'], runtime.task['worker_id'], runtime.task['fence'], runtime.task['revision'])
        if name not in self._legacy:
            self.engine.event(runtime.job_id, 'capability.started', {'tool': name, 'version': spec.version, 'digest': spec.public()['digest'], 'task_id': runtime.task['id'] if runtime.task else None})
        handler = self._handlers[name]
        result = await asyncio.wait_for(handler(arguments, runtime), spec.timeout_seconds)
        validate_value(result, spec.output_schema)
        if runtime.task:
            self.engine.store.validate_task_lease(runtime.task['id'], runtime.task['worker_id'], runtime.task['fence'], runtime.task['revision'])
        if name not in self._legacy:
            self.engine.event(runtime.job_id, 'capability.completed', {'tool': name, 'digest': spec.public()['digest'], 'task_id': runtime.task['id'] if runtime.task else None, 'result': self._brief(result)})
        return result

    @staticmethod
    def _brief(result):
        value = redact(result)
        # Large extracted text and program output remain in the node receipt,
        # not replicated into every public progress event.
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 12000:
            return {'summary': 'Output saved in the workflow receipt', 'keys': list(value)[:30]}
        return value

    def _builtins(self):
        from .contracts import ACTION_MODELS
        from .tools import TOOLS
        self._legacy = set(ACTION_MODELS)
        readonly = {'state', 'fetch', 'search', 'resolve', 'browser_open', 'browser_observe'}
        replay = {'state', 'fetch', 'search', 'resolve', 'browser_observe', 'extract', 'archive_expand', 'seal_issue', 'seal_article'}
        network = {'fetch', 'download', 'search', 'resolve', 'browser_open', 'browser_observe', 'browser_action', 'artifact_commit', 'extract', 'archive_expand', 'page_extract', 'challenge', 'handoff'}
        for name, model in ACTION_MODELS.items():
            async def legacy(args, runtime, action=name): return await runtime.execute(action, args)
            self.register(ToolSpec(name, TOOLS[name], model.model_json_schema(), read_only=name in readonly,
                execution='execution_plane' if name in network else 'coordinator', replay_safe=name in replay,
                handler_digest=canonical_digest({file:hashlib.sha256((Path(__file__).parent/file).read_bytes()).hexdigest() for file in ('tools.py','contracts.py','vault.py','browser.py', *(['provider_enrollment.py','connection_agent.py'] if name == 'provider_form' else []))})), legacy)
        self.register(ToolSpec('content.select', 'Extract matching HTML elements, text or attributes with CSS selectors. Exact extraction, no model call.',
            object_schema({'html': STRING, 'selector': STRING, 'base_url': STRING, 'attribute': STRING,
                           'fields': {'type': 'object', 'additionalProperties': STRING}, 'text_mode': {'enum': ['raw', 'normalized']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10000}}, ['html', 'selector']),
            read_only=True, replay_safe=True), self._select)
        self.register(ToolSpec('content.write', 'Persist derived text, JSON or CSV and its source references. This creates a derived artifact, not proof of an original download.',
            object_schema({'data': {}, 'format': {'enum': ['text', 'json', 'csv']}, 'filename': STRING, 'role': STRING,
                           'resource_id': STRING, 'source_artifact_ids': {'type': 'array', 'items': STRING},
                           'source_urls': {'type': 'array', 'items': STRING}}, ['data', 'format']), replay_safe=True), self._write)
        self.register(ToolSpec('content.read', 'Read a verified current-job text/JSON/CSV artifact for transformation.',
            object_schema({'artifact_id': STRING, 'max_bytes': {'type': 'integer', 'minimum': 1, 'maximum': 8000000}}, ['artifact_id']),
            read_only=True, replay_safe=True), self._read)
        self.register(ToolSpec('code.register', 'Register task-local Python or JavaScript. Program reads one JSON stdin value and prints one JSON result. Container has no network, host credentials or host filesystem. Tools/HTTP are separate workflow steps.',
            object_schema({'language': {'enum': ['python', 'javascript']}, 'source': {'type': 'string', 'minLength': 1, 'maxLength': 200000},
                           'input_schema': OBJECT, 'output_schema': OBJECT, 'description': STRING}, ['language', 'source', 'input_schema', 'output_schema']), replay_safe=True), self._register_code)
        self.register(ToolSpec('code.run', 'Run a registered program in an isolated Docker process using JSON input/output and validate its output schema. No coordinator imports or shell interpolation.',
            object_schema({'program_id': STRING, 'input': {}, 'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 120}}, ['program_id', 'input']),
            execution='sandbox', timeout_seconds=150, replay_safe=True), self._run_code)
        self.register(ToolSpec('capabilities.inspect', 'Inspect available versioned tool contracts and sandbox availability.', object_schema(), read_only=True, replay_safe=True), self._inspect)
        try:
            from .scholarly_workflow import register_scholarly_capabilities
            register_scholarly_capabilities(self)
        except ImportError:
            pass

    async def _inspect(self, args, runtime):
        from .sandbox import sandbox_availability
        profiles = self.engine.profiles() if hasattr(self.engine, 'profiles') else []
        runes = self.engine.runes() if hasattr(self.engine, 'runes') else []
        return {'capabilities': self.catalog(), 'plugin_errors': self.plugin_errors, 'sandbox': await sandbox_availability(),
                'access_profiles': [{key: profile[key] for key in ('id','name','retrieval_policy','external_model_content') if key in profile} for profile in profiles],
                'site_contexts': [{'id': rune.get('protocol_id'), 'context': rune.get('context'),
                                   'instruction': 'Site context only: explicit user dates, targets and outputs take precedence over bundled defaults.'} for rune in runes],
                'source_readiness': self.engine.source_readiness(runtime.job()['mission']) if hasattr(self.engine, 'source_readiness') else []}

    async def _select(self, args, runtime):
        soup = BeautifulSoup(args['html'], 'html.parser')
        for element in soup(['script', 'style', 'noscript']): element.decompose()
        all_nodes = soup.select(args['selector'])
        rows = []
        for node in all_nodes[:args.get('limit', 10000)]:
            row = {'text': node.get_text() if args.get('text_mode') == 'raw' else node.get_text(' ', strip=True)}
            attribute = args.get('attribute')
            if attribute:
                value = node.get(attribute)
                row['value'] = urljoin(args.get('base_url', ''), value) if attribute in ('href', 'src') and isinstance(value, str) else value
            for name, selector in args.get('fields', {}).items():
                target = node.select_one(selector)
                row[name] = target.get_text(' ', strip=True) if target else None
            rows.append(row)
        return {'items': rows, 'matched': len(all_nodes), 'returned': len(rows), 'truncated': len(rows) < len(all_nodes),
                'source_url': args.get('base_url'), 'text_mode': args.get('text_mode', 'normalized'), 'input_sha256': hashlib.sha256(args['html'].encode()).hexdigest()}

    async def _write(self, args, runtime):
        import csv
        import io
        data, fmt = args['data'], args['format']
        if fmt == 'text':
            if not isinstance(data, str): raise ValueError('Text output requires a string')
            content, media = data.encode(), 'text/plain'
        elif fmt == 'json': content, media = json.dumps(data, ensure_ascii=False, indent=2).encode(), 'application/json'
        else:
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data): raise ValueError('CSV output requires an array of objects')
            buffer = io.StringIO(newline=''); fields = list(dict.fromkeys(key for row in data for key in row))
            writer = csv.DictWriter(buffer, fieldnames=fields); writer.writeheader(); writer.writerows(data)
            content, media = buffer.getvalue().encode(), 'text/csv'
        if len(content) > 8_000_000: raise ValueError('Derived artifact exceeds transfer limit')
        store, job = self.engine.store, runtime.job()
        scope = f"{runtime.job_id}:{job['revision']}:{job['generation']}"
        if not store.reserve_budget(scope, 'derived_bytes', len(content), job['mission'].get('budget', {}).get('max_bytes', 1_000_000_000))['allowed']:
            raise AccessDenied('Derived output byte budget exhausted')
        sources = args.get('source_artifact_ids', [])
        artifacts = {row['id']: row for row in store.artifacts(runtime.job_id)}
        if any(ident not in artifacts or artifacts[ident].get('status') != 'verified' for ident in sources):
            raise AccessDenied('Derived input must be a verified artifact in this job')
        for ident in sources: self._verified_path(artifacts[ident])
        digest = hashlib.sha256(content).hexdigest()
        path = self.engine.vault.blob_path(digest); path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest: raise AccessDenied('Stored output hash mismatch')
        else:
            import os
            import tempfile
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='derived-')
            try:
                with os.fdopen(fd, 'wb') as stream: stream.write(content)
                try: os.link(temporary, path)
                except FileExistsError: pass
            finally: Path(temporary).unlink(missing_ok=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest: raise AccessDenied('Output changed during storage')
        filename = Path(args.get('filename') or f'result.{"txt" if fmt == "text" else fmt}').name
        resource_id = args.get('resource_id')
        if resource_id and not any(row['id'] == resource_id for row in store.resources(runtime.job_id)):
            raise AccessDenied('Output resource belongs to another job')
        record = {'id': canonical_digest([runtime.job_id, digest, resource_id, args.get('role', 'derived')]), 'path': str(path), 'sha256': digest,
                  'bytes': len(content), 'media_type': media, 'filename': filename, 'role': args.get('role', 'derived'),
                  'resource_id': resource_id, 'status': 'verified', 'integrity': 'verified', 'identity': 'not_required',
                  'derivation': {'kind': 'content.write', 'source_artifact_ids': sources, 'source_urls': args.get('source_urls', [])},
                  'original_download': False}
        artifact = store.add_artifact(runtime.job_id, record, lease=runtime.lease)
        return {'artifact': {key: value for key, value in artifact.items() if key != 'path'}}

    def _verified_path(self, artifact):
        path = Path(artifact['path'])
        if path.is_symlink() or not path.resolve().is_relative_to(self.engine.vault.root.resolve()) or not path.is_file():
            raise AccessDenied('Artifact path is invalid')
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']: raise AccessDenied('Artifact hash mismatch')
        return path

    async def _read(self, args, runtime):
        artifact = next((row for row in self.engine.store.artifacts(runtime.job_id) if row['id'] == args['artifact_id']), None)
        if not artifact or artifact.get('status') != 'verified': raise AccessDenied('Unknown verified artifact')
        path = self._verified_path(artifact)
        limit = args.get('max_bytes', 1_000_000)
        if path.stat().st_size > limit: raise ValueError('Artifact exceeds requested read limit')
        text = path.read_bytes().decode('utf-8')
        return {'artifact_id': artifact['id'], 'sha256': artifact['sha256'], 'text': text,
                'data': json.loads(text) if artifact.get('media_type') == 'application/json' else None}

    async def _register_code(self, args, runtime):
        validate_schema(args['input_schema']); validate_schema(args['output_schema'])
        from .sandbox import resolve_image, resource_mode
        image = await resolve_image(args['language'])
        value = {**args, 'image': image, 'network': 'none', 'permissions': ['json_input', 'json_output'], 'version': 1, 'resource_mode': resource_mode()}
        ident = canonical_digest(value)
        existing = self.engine.store.get_document('capability.program', [runtime.job_id, ident])
        if not existing:
            self.engine.store.put_document('capability.program', [runtime.job_id, ident], {'id': ident, **value}, job_id=runtime.job_id, lease=runtime.lease, expected_version=0)
        return {'program_id': ident, 'image': image, 'digest': ident, 'input_schema': args['input_schema'], 'output_schema': args['output_schema'], 'network': 'none'}

    async def _run_code(self, args, runtime):
        program = self.engine.store.get_document('capability.program', [runtime.job_id, args['program_id']])
        if not program: raise AccessDenied('Program is not registered in this job')
        value = {key: program[key] for key in ('language', 'source', 'input_schema', 'output_schema', 'image', 'network', 'permissions', 'version', 'resource_mode')}
        if 'description' in program: value['description'] = program['description']
        if canonical_digest(value) != args['program_id']: raise AccessDenied('Program manifest digest changed')
        validate_value(args['input'], program['input_schema'])
        from .sandbox import run_program
        result = await run_program(program, args['input'], timeout=args.get('timeout_seconds', 30), staging_root=self.engine.settings.state_dir / 'code-staging')
        validate_value(result['output'], program['output_schema'])
        return {**result, 'program_id': args['program_id'], 'output_verified': True}
