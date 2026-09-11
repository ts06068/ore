#!/usr/bin/env python3
"""Bounded metadata/OA preflight; candidates are not validated article files.

Explicit --state-dir and --profile are required. This reads the selected access
profile and only its credential references; it never starts Engine, a browser,
Codex, or a collection job. Existing SQLite origin-rate reservations are used.
PMC may read public JATS XML to inspect supplement relationships. PDFs and
supplement bodies are never fetched. Use --dry-run for a network-free plan.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from ore.config import SecretStore
from ore.policy import AccessDenied, AccessPolicy, GuardedTransport, RateLimiter, redact
from ore.source_policy import authorize_operation
from ore.store import Store
from ore_scholarly import resolve, search
from ore_scholarly.registry import SOURCES

JOURNALS = (
    {'id': 'jacc', 'journal': 'Journal of the American College of Cardiology', 'volume': '83', 'issue': '1'},
    {'id': 'ehj', 'journal': 'European Heart Journal', 'volume': '45', 'issue': '1'},
    {'id': 'circulation', 'journal': 'Circulation', 'volume': '149', 'issue': '1'},
    {'id': 'jama-cardiology', 'journal': 'JAMA Cardiology', 'volume': '9', 'issue': '1'},
)
REGISTRY = {row['id']: row for row in SOURCES}
UNCONFIGURED_REF = '__ore_preflight_credential_not_configured__'
EXCLUDED_INDEX_TYPES = {'letter', 'comment', 'editorial', 'review', 'systematic review', 'meta-analysis',
    'published erratum', 'guideline', 'practice guideline', 'retracted publication', 'retraction of publication',
    'expression of concern', 'news', 'interview', 'historical article', 'biography', 'congress', 'consensus development conference'}
RESEARCH_INDEX_TYPES = {'clinical trial', 'randomized controlled trial', 'controlled clinical trial',
    'observational study', 'comparative study', 'multicenter study', 'evaluation study', 'validation study'}


def indexed_exclusions(record):
    return sorted({str(value).strip().lower() for value in record.get('article_type', [])} & EXCLUDED_INDEX_TYPES)


def research_priority(record):
    types = {str(value).strip().lower() for value in record.get('article_type', [])}
    return (bool(types & RESEARCH_INDEX_TYPES), any(value.startswith('research support,') for value in types))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def safe_url(value):
    parsed = urlsplit(str(value or ''))
    host = parsed.hostname or ''
    if parsed.port:
        host += ':' + str(parsed.port)
    return redact(urlunsplit((parsed.scheme, host, parsed.path, parsed.query, '')))


def exact_query(journal):
    return f'"{journal["journal"]}"[Journal] AND 2024[Date - Publication] AND {journal["volume"]}[Volume] AND {journal["issue"]}[Issue]'


def scope_match(record, journal):
    normalize = lambda value: ' '.join(str(value or '').lower().split())
    return (normalize(record.get('journal')) == normalize(journal['journal'])
            and str(record.get('volume', '')).strip() == journal['volume']
            and str(record.get('issue', '')).strip() == journal['issue']
            and str(record.get('year')) == '2024')


def candidate_summary(result):
    candidates, identities = [], set()
    for row in result.get('candidates', []):
        identity = (row.get('role'), row.get('url'), row.get('version'), row.get('article_version'))
        if identity in identities:
            continue
        identities.add(identity)
        value = {key: row[key] for key in ('source', 'role', 'version', 'article_version', 'pmcid', 'doi',
            'license', 'is_manuscript', 'external_reference', 'version_evidence') if key in row}
        value.update(url=safe_url(row.get('url')), file_validated=False)
        candidates.append(value)
    counts = Counter(row.get('role', 'unknown') for row in candidates)
    return {'status': result.get('status', 'unknown'), 'candidate_counts': {
        'main_pdf': counts['main_pdf'], 'supplement': counts['supplement'],
        'other': sum(number for role, number in counts.items() if role not in ('main_pdf', 'supplement'))},
        'candidates': candidates, 'supplement_status': result.get('supplement_status'),
        'observations': [{key: row[key] for key in ('article_version', 'main_pdf_status', 'supplement_status') if key in row}
                         for row in result.get('observations', [])],
        'issue_codes': sorted({row.get('code', 'unknown') for row in result.get('issues', [])}),
        'candidate_is_not_validated_file': True}


class TransferBudget:
    def __init__(self):
        self.requests, self.bytes, self.events = 0, 0, []


class CountedStream(httpx.AsyncByteStream):
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget
    async def __aiter__(self):
        async for chunk in self.stream:
            self.budget.bytes += len(chunk)
            if self.budget.bytes > 32 * 1024 * 1024:
                raise AccessDenied('Preflight metadata byte limit reached')
            yield chunk
    async def aclose(self):
        await self.stream.aclose()


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport, budget):
        self.transport, self.budget = transport, budget
    async def handle_async_request(self, request):
        if request.method != 'GET' or self.budget.requests >= 64:
            raise AccessDenied('Preflight permits at most 64 metadata GET requests')
        self.budget.requests += 1
        part = urlsplit(str(request.url))
        event = {'method': 'GET', 'endpoint': urlunsplit((part.scheme, part.hostname or '', part.path, '', ''))}
        self.budget.events.append(event)
        response = await self.transport.handle_async_request(request)
        event['http_status'] = response.status_code
        response.stream = CountedStream(response.stream, self.budget)
        return response
    async def aclose(self):
        await self.transport.aclose()


class Preflight:
    def __init__(self, state_dir, profile, timeout, records_per_journal=5):
        self.state_dir, self.profile, self.timeout = state_dir, profile, timeout
        self.records_per_journal = records_per_journal
        self.secret_store = self.store = None
        self.budget = TransferBudget()
        self.mission = {'sources': ['pubmed'], 'source_policy': {'allow': {
            'search': ['pubmed'], 'resolve': ['pmc', 'unpaywall'], 'browser': [], 'download': [], 'import': []}}}
        self.disabled = {}

    def admission(self, source, operation):
        value = authorize_operation(self.mission, self.profile, source, operation)
        return {'allowed': value['allowed'], 'code': value['code'], 'state': value['readiness']['state'],
                'source': source, 'operation': operation}

    def configuration(self, source):
        row = REGISTRY[source]
        configured = self.profile.get('sources', {}).get(source, {})
        names = set(row.get('credentials', {})) | set(row.get('optional_credentials', {}))
        refs = {name: configured.get(name + '_ref') for name in names}
        allowed = {ref for ref in refs.values() if isinstance(ref, str) and ref}
        def get_secret(ref):
            if ref not in allowed:
                return None
            if self.secret_store is None:
                if not (self.state_dir / 'secret.key').is_file() and not os.environ.get('ORE_SECRET_KEY'):
                    raise AccessDenied('Selected state directory has no configured secret key')
                self.secret_store = SecretStore(self.state_dir)
            return self.secret_store.get(ref)
        for name in row.get('credentials', {}):
            if not refs.get(name) or not get_secret(refs[name]):
                return None
        # Absent optional refs explicitly return None instead of falling back to
        # unrelated ambient NCBI_EMAIL/API_KEY environment variables.
        config = {name + '_ref': refs.get(name) or UNCONFIGURED_REF for name in names}
        config.update(secret_resolver=get_secret, version_selection='latest', max_version_pages=1, inspect_jats=True)
        return config

    async def operation(self, source, operation, value):
        decision = self.admission(source, operation)
        if not decision['allowed']:
            return {'status': 'skipped', 'admission': decision, 'network_requests': 0}
        if source in self.disabled:
            return {'status': 'skipped', 'code': self.disabled[source], 'network_requests': 0}
        before = self.budget.requests
        try:
            config = self.configuration(source)
            if config is None:
                return {'status': 'skipped', 'code': 'credential_reference_unconfigured', 'network_requests': 0}
            policy = AccessPolicy(self.mission, self.profile, operation=operation, source_hint=source)
            transport = ReadOnlyTransport(httpx.AsyncHTTPTransport(proxy=self.profile.get('proxy')), self.budget)
            guarded = GuardedTransport(policy, RateLimiter(self.store), max(1.0, float(self.profile.get('api_interval', 1))),
                profile_id=self.profile['id'], transport=transport)
            async with httpx.AsyncClient(transport=guarded, trust_env=False, follow_redirects=False,
                timeout=30, headers={'User-Agent': 'ORE-Scholarly/0.2 bounded metadata preflight'}) as client:
                config['client'] = client
                async with asyncio.timeout(60):
                    result = await search(source, value, limit=self.records_per_journal, config=config) if operation == 'search' else await resolve(source, value, config=config)
            return {'status': 'observed', 'result': result, 'network_requests': self.budget.requests - before}
        except (Exception, asyncio.TimeoutError) as exc:
            code = getattr(exc, 'code', type(exc).__name__)
            if code in ('rate_limited', 'access_required', 'credentials_missing', 'response_too_large') or isinstance(exc, AccessDenied):
                self.disabled[source] = code
            return {'status': 'failed', 'code': code, 'http_status': getattr(exc, 'status', None),
                    'network_requests': self.budget.requests - before}

    async def run(self, report):
        database = self.state_dir / 'ore.db'
        if not database.is_file():
            raise ValueError('The explicitly selected state directory must contain its existing ore.db rate database')
        self.store = Store('sqlite:///' + str(database))
        try:
            async with asyncio.timeout(self.timeout):
                for journal in JOURNALS:
                    item = {**journal, 'year': 2024, 'query': exact_query(journal), 'records': [], 'resolutions': []}
                    report['journals'].append(item)
                    found = await self.operation('pubmed', 'search', item['query'])
                    data = found.pop('result', {})
                    item['search'] = {**found, 'provider_total': data.get('total'), 'returned_records': len(data.get('records', [])),
                        'continuation_available': bool(data.get('next_cursor')), 'original_research_classification': 'unverified'}
                    print(json.dumps({'journal': journal['id'], 'phase': 'pubmed', 'status': found['status'], 'records': len(data.get('records', []))}), flush=True)
                    selected = []
                    for row in data.get('records', [])[:self.records_per_journal]:
                        record = {key: row.get(key) for key in ('pmid', 'pmcid', 'doi', 'title', 'journal', 'year', 'volume', 'issue', 'article_type')}
                        record['exact_scope_match'] = scope_match(row, journal)
                        record['excluded_by_index_type'] = indexed_exclusions(record)
                        record['original_research_classification'] = 'excluded_by_index_type' if record['excluded_by_index_type'] else 'unverified'
                        item['records'].append(record)
                    candidates = sorted((record for record in item['records'] if record['exact_scope_match']
                        and not record['excluded_by_index_type'] and record.get('doi')), key=research_priority, reverse=True)
                    for record in candidates:
                        if record['doi'] not in selected and len(selected) < 2:
                            selected.append(record['doi'])
                    item['research_candidate_records'] = len(candidates)
                    item['selected_dois'] = selected
                    item['exact_scope_records'] = sum(record['exact_scope_match'] for record in item['records'])
                    print(json.dumps({'journal': journal['id'], 'phase': 'scope', 'selected_dois': selected, 'original_research_classification': 'unverified'}), flush=True)
                    for identifier in selected:
                        for source in ('pmc', 'unpaywall'):
                            outcome = await self.operation(source, 'resolve', identifier)
                            result = outcome.pop('result', None)
                            item['resolutions'].append({'doi': identifier, 'source': source, **outcome,
                                **({'observation': candidate_summary(result)} if result is not None else {})})
                            print(json.dumps({'journal': journal['id'], 'phase': 'resolve', 'doi': identifier, 'source': source, 'status': outcome['status'], 'code': outcome.get('code'), 'observation': candidate_summary(result) if result is not None else None}), flush=True)
        except asyncio.TimeoutError:
            report['deadline_reached'] = True
        finally:
            self.store.close()
            report['http_requests'] = self.budget.requests
            report['response_bytes'] = self.budget.bytes
            report['request_log'] = self.budget.events
        incomplete = len(report['journals']) != len(JOURNALS) or report.get('deadline_reached') or any(
            row.get('search', {}).get('status') != 'observed'
            or (not row.get('selected_dois') and row.get('search', {}).get('provider_total') != 0)
            or any(item['status'] != 'observed' for item in row['resolutions'])
            for row in report['journals'])
        report['status'] = 'partial_preflight' if incomplete else 'completed_preflight'
        total = Counter()
        for journal in report['journals']:
            counts = Counter(candidate.get('role', 'unknown') for result in journal['resolutions']
                for candidate in result.get('observation', {}).get('candidates', []))
            journal['candidate_counts'] = {'main_pdf': counts['main_pdf'], 'supplement': counts['supplement'],
                'other': sum(number for role, number in counts.items() if role not in ('main_pdf', 'supplement'))}
            total.update(journal['candidate_counts'])
        report['candidate_counts'] = {role: total[role] for role in ('main_pdf', 'supplement', 'other')}
        report['candidate_count_unit'] = 'resolver_candidate_records_in_sample; not unique or validated files'
        return report


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='api-preflight-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2); stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def main(args):
    state_dir = args.state_dir.expanduser().resolve(strict=True)
    profiles = json.loads((state_dir / 'access-profiles.json').read_text())
    profile = next((row for row in profiles if row.get('id') == args.profile), None)
    if profile is None:
        raise ValueError('Explicitly selected access profile does not exist')
    runner = Preflight(state_dir, profile, args.timeout, args.records_per_journal)
    report = {'schema_version': 'ore.api-fallback-preflight/v1', 'started_at': utcnow(), 'status': 'planned',
        'profile_id': args.profile, 'state_directory': str(state_dir), 'sample': True,
        'inventory_status': 'inventory_unverified', 'global_recall': 'unknown', 'validated_files': 0,
        'candidate_is_not_validated_file': True, 'original_research_classification': 'unverified',
        'resolver_selection': 'exclude explicitly nonoriginal index types; prioritize study and research-support types; journal eligibility remains unverified',
        'limits': {'records_per_journal': args.records_per_journal, 'dois_per_journal': 2, 'http_requests': 64,
                   'response_bytes': 32 * 1024 * 1024, 'seconds': args.timeout},
        'source_admission': [runner.admission(source, operation) for source, operation in
                             [('pubmed', 'search'), ('pmc', 'resolve'), ('unpaywall', 'resolve')]],
        'browser_started': False, 'model_calls': 0, 'existing_jobs_modified': False,
        'profile_or_readiness_modified': False, 'journals': []}
    if args.dry_run:
        report['journals'] = [{**row, 'query': exact_query(row)} for row in JOURNALS]
        report['credentials_resolved'] = False
    else:
        await runner.run(report)
    report['finished_at'] = utcnow()
    destination = args.report or state_dir / 'reports' / ('api-fallback-plan.json' if args.dry_run else 'api-fallback-preflight.json')
    write_report(destination, report)
    print(json.dumps({'status': report['status'], 'report': str(destination),
        'candidate_counts': report.get('candidate_counts', {}), 'validated_files': 0, 'inventory_status': 'inventory_unverified'}))
    return 2 if report['status'] == 'partial_preflight' else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--timeout', type=float, default=240)
    parser.add_argument('--records-per-journal', type=int, default=5)
    parser.add_argument('--dry-run', action='store_true')
    options = parser.parse_args()
    if not 1 <= options.records_per_journal <= 50:
        parser.error('--records-per-journal must be between 1 and 50')
    if not 1 <= options.timeout <= 600:
        parser.error('--timeout must be between 1 and 600 seconds')
    try:
        raise SystemExit(asyncio.run(main(options)))
    except (OSError, ValueError) as error:
        parser.exit(2, 'Preflight configuration failed: ' + type(error).__name__ + '\n')
