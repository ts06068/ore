"""Private, finite architecture-evaluation sources; never a publisher-access claim.

The actor only receives ``prompt`` and the shared tool surface. Seed material,
source generator state and exact expected outputs belong to the host grader.
Tests of this module use explicit test seeds, not the official holdout seed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import random
import re
import zipfile

try:
    from .benchmark_fixtures import BenchmarkFixture, FAMILIES as FAMILIES, S, obj
except ImportError:
    from benchmark_fixtures import BenchmarkFixture, FAMILIES as FAMILIES, S, obj

LIFECYCLE_FAMILIES = ('main_and_supplements', 'repeated_extraction', 'restart_failure')
SCALES = {'paragraph': 1, 'paginated_list': 100, 'main_and_supplements': 100,
          'repeated_extraction': 1000, 'restart_failure': 1000, 'verification_rate_limit': 2}
WORKSPACE_LIMIT = 2_000_000


class SharedWorkspace:
    """Both actors retain only their own generated source/recipes across requests."""
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def specs():
        return [
            {'name': 'bench.workspace_write', 'description': 'Save your own generated code, recipe or notes for later requests. UTF-8 text only, no execution or source access. Both comparison arms have this same persistent workspace.',
             'inputSchema': obj({'name': S, 'text': S}, ['name', 'text'])},
            {'name': 'bench.workspace_read', 'description': 'Read your own previously saved workspace text. No hidden source, answer or other actor state is accessible.',
             'inputSchema': obj({'name': S}, ['name'])},
            {'name': 'bench.workspace_list', 'description': 'List your own retained generated-code/recipe filenames and SHA-256 digests.', 'inputSchema': obj({})},
        ]

    def _path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', name):
            raise ValueError('Use one ordinary workspace filename')
        path = self.root / name
        if path.is_symlink():
            raise ValueError('Workspace symlinks are not permitted')
        return path

    async def call(self, name, args):
        if name == 'bench.workspace_list':
            return {'files': [{'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'bytes': path.stat().st_size} for path in sorted(self.root.iterdir())
                              if path.is_file() and not path.is_symlink()]}
        path = self._path(args['name'])
        if name == 'bench.workspace_write':
            text = args['text']
            if not isinstance(text, str):
                raise ValueError('Workspace contents must be UTF-8 text')
            data = text.encode()
            used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file() and not p.is_symlink() and p != path)
            if used + len(data) > WORKSPACE_LIMIT:
                raise ValueError('Generated workspace exceeds shared 2MB limit')
            path.write_bytes(data)
            return {'saved': True, 'name': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        if name == 'bench.workspace_read':
            data = path.read_bytes()
            return {'name': path.name, 'text': data.decode(), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        raise ValueError('Unknown workspace tool')


class EvaluationFixture(BenchmarkFixture):
    """Legacy sources or held-out source shapes, with equal persistent workspaces."""
    def __init__(self, family, repetition, directory, *, split='regression', phase='cold',
                 seed_material: bytes, workspace: Path, count_override=None):
        super().__init__(family, repetition, directory)
        if split not in ('regression', 'holdout') or phase not in ('cold', 'unchanged', 'changed'):
            raise ValueError('Unknown evaluation split or phase')
        self.split, self.phase = split, phase
        self._seed_material = seed_material
        self._count_override = count_override
        self.workspace = SharedWorkspace(workspace)
        self.tool_specs.extend(self.workspace.specs())
        self._phase_number = ('cold', 'unchanged', 'changed').index(phase)
        self._retry_paths = set()
        self._verification_path = '/access/form'
        self._confirmation_path = '/access/confirm'
        self._limited_path = '/limited/content'
        self._verification_code = ''
        self.data_items = 0

    def _build(self):
        if self.split == 'regression':
            super()._build()
            self.data_items = 12 if self.family in ('repeated_extraction', 'restart_failure') else len(self._expected)
        else:
            self._build_holdout()
        self.prompt += (' You may generate reusable code or protocols and persist them with bench.workspace_write; '
                        'read/list your retained workspace on later requests. Native code orchestration may batch the same '
                        'raw primitives with at most 5 in flight. Original source content and expected outputs are not '
                        'present in the workspace. All preparation, verification and repair costs count.')
        if self.phase != 'cold':
            self.prompt += (' This is a new data request for the same source family. Reuse your own prior programs or '
                            'protocols when their assumptions remain valid. The starting origin below is authoritative; '
                            'old origin URLs may be expired. Inspect the current source before claiming completion.')

    def _build_holdout(self):
        seed = int.from_bytes(hashlib.sha256(self._seed_material + self.family.encode() + str(self.repetition).encode()).digest(), 'big')
        rng = random.Random(seed)
        variant = self.phase == 'changed'
        count = self._count_override if self._count_override is not None else SCALES[self.family]
        self.data_items = count
        self.restart_after_saves = 2 if self.family == 'restart_failure' else None
        prefix = hashlib.sha256(self._seed_material + self.family.encode()).hexdigest()[:8]
        nonce = hashlib.sha256(self._seed_material + self.phase.encode() + str(self.repetition).encode()).hexdigest()[:12]
        common = ('Use only the supplied bench primitives and your own generated code. Preserve exact requested '
                  'bytes and whitespace. Inspect observed links, never guess a hidden URL or treat HTTP errors as data. ')
        if self.family == 'paragraph':
            value = f'{nonce}:  재현 가능한 원문 수집\nCohort {rng.randrange(1000,9999)} — preserve  these spaces.'
            self._add('/', f'<main><section aria-label="Overview"><h2>Methods</h2><p>Unrelated summary.</p></section>'
                      f'<section data-section="study-design"><header><h2>Study methods</h2></header><div><p data-primary="true">{value}</p>'
                      '<p>Supplemental note, not the requested paragraph.</p></div></section></main>')
            self._expected = {'methods.txt': value.encode()}
            goal = 'Save the first paragraph in the section whose heading is Study methods as methods.txt, retaining all whitespace.'
        elif self.family == 'paginated_list':
            records = [f'{nonce}-entry-{i:04d}' for i in range(count)]
            page_size = 13
            paths = ['/' if i == 0 else f'/browse/{prefix}/{i}' for i in range((count + page_size - 1) // page_size)]
            for page, path in enumerate(paths):
                next_link = f'<nav><a aria-label="Next results page" href="{paths[page+1]}">Continue</a></nav>' if page + 1 < len(paths) else '<p data-pagination="complete">No more results</p>'
                self._add(path, '<main><table><tbody>' + ''.join(f'<tr data-record="true"><td class="identifier">{v}</td><td>active</td></tr>' for v in records[page*page_size:(page+1)*page_size]) + '</tbody></table>' + next_link + '</main>')
            self._expected = {'records.json': records}
            goal = 'Traverse all results pages. Save all data-record row identifiers, in page order, as the JSON array records.json.'
        elif self.family == 'main_and_supplements':
            from pypdf import PdfWriter
            links = []
            for i in range(1, count + 1):
                article = f'/research/{prefix}/{i}'
                links.append(f'<li><a rel="article" href="{article}">Study {i:04d}</a><span>Original Research</span></li>')
                writer = PdfWriter(); writer.add_blank_page(width=72, height=72)
                writer.add_metadata({'/Title': f'{nonce} study {i:04d}'})
                buffer = io.BytesIO(); writer.write(buffer)
                csv = f'study,value\n{i},{rng.randrange(100000)}-{self._phase_number}\n'.encode()
                zipped = io.BytesIO()
                with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(zipfile.ZipInfo('README.txt', (2024, 1, 1, 0, 0, 0)), f'{nonce} supplement {i}\n')
                files = [(f'study-{i:04d}.pdf', buffer.getvalue(), 'application/pdf'),
                         (f'study-{i:04d}-table.csv', csv, 'text/csv'),
                         (f'study-{i:04d}-data.zip', zipped.getvalue(), 'application/zip')]
                elements = []
                for pos, (name, content, mime) in enumerate(files):
                    path = f'/files/{prefix}/{name}'
                    self._add(path, content, mime); self._expected[name] = content
                    elements.append(f'<a data-file-role="{"article" if pos == 0 else "supplement"}" href="{path}">{name}</a>')
                if variant:
                    supplement_path = article + '/supporting'
                    self._add(supplement_path, '<section aria-label="Supporting information">' + ''.join(elements[1:]) + '</section>')
                    self._add(article, '<article><h1>Original Research</h1>' + elements[0] + f'<a rel="supporting-information" href="{supplement_path}">Supporting information</a></article>')
                else:
                    self._add(article, '<article><h1>Original Research</h1><section aria-label="Files">' + ''.join(elements) + '</section></article>')
            # Editorials are visible inventory entries, but not requested artifacts.
            self._add('/editorial', '<article><h1>Editorial</h1><p>Not original research; no requested attachments.</p></article>')
            self._add('/', '<main><h1>Issue inventory</h1><ol>' + ''.join(links) + '<li><a rel="article" href="/editorial">Editorial</a><span>Editorial</span></li></ol></main>')
            goal = ('Inspect this issue inventory, visit its article entries, and download every main PDF and linked supplementary file '
                    'for Original Research entries only. Follow supporting-information pages where present; preserve original basenames and exact bytes.')
        elif self.family in ('repeated_extraction', 'restart_failure'):
            route = '/document/' if variant else '/records/'
            selector = 'data-result' if variant else 'data-measurement'
            links = []
            for i in range(1, count + 1):
                path = f'{route}{prefix}/{i}'
                name = f'record-{i:04d}.txt'
                value = f'{nonce}: record {i:04d}; observation={rng.randrange(1000000)}; phase={self._phase_number}'
                links.append(f'<li><a data-output-name="{name}" href="{path}">Record {i:04d}</a></li>')
                self._add(path, f'<article><header>Measured result</header><p>Context, not the result.</p><div><p {selector}="primary">{value}</p></div></article>')
                self._expected[name] = value.encode()
                if self.family == 'restart_failure' and i in (7, max(8, count // 2)):
                    self._retry_paths.add(path)
            self._add('/', f'<main><h1>Observation inventory</h1><p>The measured result appears in the {selector} element on each record page.</p><ul>' + ''.join(links) + '</ul></main>')
            goal = ('Visit every record link and save its measured-result paragraph exactly, using that link\'s data-output-name as the filename. '
                    'The source inventory supplies the extraction convention. Reusable bounded extraction is permitted.')
            if self.family == 'restart_failure':
                goal += ' Some pages return a transient 503 with Retry-After. The harness interrupts after two saves, then resumes; preserve completed files and finish missing work.'
        else:
            self._verification_code = f'observed-{nonce}'
            self._expected = {'verified.txt': f'{nonce}: authorized fixture body'.encode(), 'limited.txt': f'{nonce}: available after retry'.encode()}
            self._add('/', f'<main><a data-output-name="verified.txt" href="{self._verification_path}">Access-controlled local fixture</a>'
                      f'<a data-output-name="limited.txt" href="{self._limited_path}">Temporary rate limit</a></main>')
            goal = ('Save the exact successful text bodies of the two linked resources to their supplied filenames. '
                    'Read and submit the visible local form if needed, then re-read the source. Respect Retry-After. '
                    'Use at most three verification submissions within 120 seconds from first observing the form.')
        self.required_paths = set(self._routes)
        if self.family == 'verification_rate_limit':
            self.required_paths.update((self._verification_path, self._limited_path))
        self.prompt = common + goal + ' Start at ' + self.origin + '/. All fixture URLs on this origin are authorized.'

    def _response(self, method, path, raw, count):
        if self.split == 'regression':
            return super()._response(method, path, raw, count)
        if path in self._retry_paths and count == 1:
            return 503, {'Retry-After': '0.05', 'Content-Type': 'text/plain'}, b'Temporary error. Retry after the stated interval.'
        if self.family == 'verification_rate_limit':
            if method == 'POST' and path == self._confirmation_path:
                try:
                    code = json.loads(raw).get('code')
                except (ValueError, AttributeError):
                    code = None
                if code == self._verification_code:
                    self.verified = True
                    return 200, {'Content-Type': 'application/json'}, json.dumps({'verified': True, 'next': self._verification_path}).encode()
                return 403, {'Content-Type': 'text/plain'}, b'Incorrect observed form code'
            if path == self._verification_path:
                if self.verified:
                    return 200, {'Content-Type': 'text/plain'}, self._expected['verified.txt']
                return 403, {'Content-Type': 'text/html'}, f'<main><form action="{self._confirmation_path}" data-code="{self._verification_code}"><p>Visible local form</p><input type="checkbox"><button>Continue</button></form></main>'.encode()
            if path == self._limited_path:
                if count <= 2:
                    return 429, {'Retry-After': '0.05', 'Content-Type': 'text/plain'}, b'Wait before retrying'
                return 200, {'Content-Type': 'text/plain'}, self._expected['limited.txt']
        return self._routes.get(path, (404, {'Content-Type': 'text/plain'}, b'No source at this URL'))

    async def call(self, name, args):
        if name.startswith('bench.workspace_'):
            import time
            started = time.monotonic()
            entry = {'tool': name, 'started': started, 'arguments': {key: value for key, value in args.items() if key != 'text'}}
            self.calls.append(entry)
            try:
                return await self.workspace.call(name, args)
            finally:
                entry['elapsed_seconds'] = time.monotonic() - started
        return await super().call(name, args)

    def grade(self):
        if self.split == 'regression' or self.family != 'verification_rate_limit':
            result = super().grade()
        else:
            # The shared byte/required-source grader stays unchanged; normalize only
            # the new source's public verification routes for legacy protocol checks.
            routes = {self._verification_path: '/verified', self._confirmation_path: '/confirm'}
            events = self.request_events
            self.request_events = [{**event, 'path': routes.get(event['path'], event['path'])} for event in events]
            required = self.required_paths
            self.required_paths = {routes.get(path, path) for path in required}
            try:
                result = super().grade()
            finally:
                self.request_events, self.required_paths = events, required
        if self.split == 'holdout':
            cooldown = {}
            for event in self.request_events:
                if event['at'] + .01 < cooldown.get(event['path'], 0):
                    violation = 'retry_before_server_interval:' + event['path']
                    if violation not in result['protocol_violations']:
                        result['protocol_violations'].append(violation)
                if event['status'] in (429, 503) and event.get('retry_after'):
                    cooldown[event['path']] = event['at'] + float(event['retry_after'])
            result['protocol_pass'] = not result['unread_required_sources'] and not result['protocol_violations']
            result['quality_pass'] = result['byte_quality_pass'] and result['protocol_pass']
        result['source_items'] = self.data_items
        result['split'] = self.split
        result['phase'] = self.phase
        result['source_contract_digest'] = self.contract_digest()
        return result

    def contract_digest(self):
        expected = {name: value if isinstance(value, list) else hashlib.sha256(value).hexdigest()
                    for name, value in sorted(self._expected.items())}
        value = {'family': self.family, 'repetition': self.repetition, 'split': self.split,
                 'phase': self.phase, 'expected': expected, 'required_paths': sorted(self.required_paths)}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


PUBLIC_RECEIPT_VALIDATOR = 'bench.public_receipt_integrity.v1'
PUBLIC_RECEIPT_VALIDATOR_DIGEST = hashlib.sha256(b'bench.public_receipt_integrity.v1:successful-fetch-selected-values-saved-bytes:source-bound-case').hexdigest()


def public_receipt_verifier(fixture, inputs, evidence):
    """Validate observed dataflow, without consulting the private expected set.

    This is intentionally narrower than final task correctness. A source-derived
    wrong paragraph may pass integrity; only the hidden final grader judges the
    user's exact target, inventory and supplement completeness.
    """
    from bs4 import BeautifulSoup
    from ore.models import canonical_digest
    receipts = [row for row in evidence.get('receipts', []) if row.get('status') == 'completed'
                and isinstance(row.get('output'), dict)]
    fetches = [row['output'] for row in receipts if row.get('tool') == 'bench.fetch' and row['output'].get('status') == 200]
    observed_html = {row.get('text', '') for row in fetches}
    hashes = set(); sources = []
    for row in fetches:
        try:
            body = base64.b64decode(row['body_base64'], validate=True)
        except (KeyError, ValueError, TypeError):
            continue
        digest = hashlib.sha256(body).hexdigest()
        hashes.add(digest); sources.append({'url': row.get('url'), 'sha256': digest})
    calls = {canonical_digest({'name': call['tool'], 'arguments': call['arguments']}): call
             for call in fixture.calls if call.get('tool') == 'bench.select'}
    for receipt in receipts:
        if receipt.get('tool') != 'bench.select':
            continue
        call = calls.get(receipt.get('input_digest'))
        if not call or call['arguments'].get('html') not in observed_html:
            continue
        args = call['arguments']
        try:
            nodes = BeautifulSoup(args['html'], 'html.parser').select(args['selector'])
            values = [node.get(args['attribute'], '') if args.get('attribute') else node.get_text() for node in nodes]
        except (ValueError, KeyError, TypeError):
            continue
        if values != receipt['output'].get('values'):
            continue
        for value in values:
            if isinstance(value, str):
                hashes.add(hashlib.sha256(value.encode()).hexdigest())
        hashes.add(hashlib.sha256(json.dumps(values, ensure_ascii=False, indent=2).encode()).hexdigest())
    saves = [row['output'] for row in receipts if row.get('tool') == 'bench.save' and row['output'].get('saved') is True]
    verified = []
    for saved in saves:
        name = saved.get('name')
        if not isinstance(name, str) or Path(name).name != name:
            continue
        path = fixture.output_dir / name
        if not path.is_file() or path.is_symlink():
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual == saved.get('sha256') and actual in hashes:
            verified.append({'name': name, 'sha256': actual})
    passed = bool(fetches and saves and len(verified) == len(saves))
    # Bind independent verification to observed source/output evidence, not to
    # an arbitrary unused nonce supplied in the model's inputs object.
    case = canonical_digest({'sources': sorted(sources, key=lambda row: (str(row['url']), row['sha256'])),
                             'outputs': sorted(verified, key=lambda row: row['name'])})
    return {'passed': passed, 'validator_digest': PUBLIC_RECEIPT_VALIDATOR_DIGEST, 'case_digest': case,
            'validation_scope': 'observed_source_to_saved_bytes_only', 'verified_outputs': len(verified)}
