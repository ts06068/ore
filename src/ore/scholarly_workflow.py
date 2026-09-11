"""Optional scholarly capabilities on top of the general workflow registry.

An archive profile is an installed/reviewed parser contract, not an agent's list
of purported issues. The ledger rehashes captured source bytes and verifies the
archive's range, all rows, and pagination before locking the denominator.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

from .coverage import CoverageError, canonical_url
from .models import canonical_digest


def parse_date(text, formats=()):
    value = str(text or '').strip()
    try: return date.fromisoformat(value[:10])
    except ValueError: pass
    for fmt in formats:
        try: return datetime.strptime(value, fmt).date()
        except ValueError: pass
    raise CoverageError('Archive issue date is missing or unrecognized')


def seal_archive(ledger, job_id, snapshot_ids, profile_id, start, until_exclusive, *, lease=None):
    start, end = date.fromisoformat(start), date.fromisoformat(until_exclusive)
    if start >= end: raise CoverageError('Archive date interval must be increasing')
    profile = ledger._profile(profile_id, ('html_archive',))
    captures = ledger._snapshots(job_id, snapshot_ids, profile)
    observed = {row['url'] for row, _ in captures}
    records, next_urls, reported, ranges = {}, set(), [], []
    for snapshot, content in captures:
        if snapshot.get('authority') != 'journal': raise CoverageError('Archive inventory requires official journal evidence')
        soup = BeautifulSoup(content, 'html.parser')
        container = soup.select_one(profile['container_selector'])
        if container is None or not soup.select(profile['complete_selector']): raise CoverageError('Archive template is incomplete or unrecognized')
        if profile.get('pending_selector') and soup.select(profile['pending_selector']): raise CoverageError('Archive still contains unresolved content')
        range_node = soup.select_one(profile['range_selector'])
        if range_node is None: raise CoverageError('Official archive range evidence is missing')
        first = parse_date(range_node.get(profile.get('range_start_attribute', 'data-from')), profile.get('date_formats', []))
        last = parse_date(range_node.get(profile.get('range_end_attribute', 'data-until-exclusive')), profile.get('date_formats', []))
        if first >= last: raise CoverageError('Official archive range is invalid')
        ranges.append((first, last))
        for node in soup.select(profile.get('next_selector', 'a[rel="next"]')):
            if node.get('href'): next_urls.add(canonical_url(urljoin(snapshot['url'], node['href'])))
        seen = set()
        for row in container.select(profile['row_selector']):
            link = row.select_one(profile['link_selector'])
            time = row.select_one(profile['date_selector'])
            if link is None or not link.get('href') or time is None: raise CoverageError('Archive row lacks an issue link or date')
            url = canonical_url(urljoin(snapshot['url'], link['href']))
            if urlsplit(url).hostname not in profile['origins']: raise CoverageError('Archive issue URL is outside its trusted origins')
            issue_date = parse_date(time.get(profile.get('date_attribute', 'datetime')) or time.get_text(' ', strip=True), profile.get('date_formats', []))
            if not first <= issue_date < last: raise CoverageError('Issue date conflicts with the archive range')
            record = {'url': url, 'issue_date': issue_date.isoformat(), 'title': link.get_text(' ', strip=True), 'snapshot_id': snapshot['id']}
            if url in records and records[url]['issue_date'] != record['issue_date']: raise CoverageError('Conflicting dates for the same issue')
            records.setdefault(url, record); seen.add(url)
        pattern = profile.get('issue_url_pattern')
        if not pattern: raise CoverageError('Archive profile requires an issue URL pattern')
        all_links = {canonical_url(urljoin(snapshot['url'], a['href'])) for a in container.select('a[href]') if re.search(pattern, a['href'])}
        if all_links != seen: raise CoverageError('Unaccounted issue links remain in the archive')
        counter = soup.select_one(profile['count_selector']) if profile.get('count_selector') else None
        if counter is not None:
            value = counter.get(profile.get('count_attribute', 'data-total-issues')) or counter.get_text(strip=True)
            if not re.fullmatch(r'\d+', str(value)): raise CoverageError('Official archive count is invalid')
            reported.append(int(value))
    if next_urls - observed: raise CoverageError('Archive pagination is not exhausted')
    cursor = start
    for first, last in sorted(ranges):
        if first <= cursor < last: cursor = last
    if cursor < end: raise CoverageError('Captured archive ranges do not cover the requested dates')
    if reported and any(value != len(records) for value in reported): raise CoverageError('Archive issue count disagrees with the extracted inventory')
    selected = sorted((row for row in records.values() if start <= date.fromisoformat(row['issue_date']) < end), key=lambda row: (row['issue_date'], row['url']))
    if not selected: raise CoverageError('No issues match the requested archive interval; verify the requested journal/date range')
    value = {'issue_ids': sorted(row['url'] for row in selected), 'issues': selected, 'evidence_snapshot_ids': sorted(snapshot_ids),
             'selection_rule': {'basis': 'issue_date', 'from': start.isoformat(), 'until_exclusive': end.isoformat()},
             'target_basis': 'verified_official_archive', 'reviewer': profile.get('reviewer', 'installed_profile'),
             'profile_id': profile_id, 'profile_digest': canonical_digest(profile), 'pagination_complete': True,
             'archive_issues_observed': len(records)}
    return ledger._put(job_id, 'collection', 'target', value, lease)


def register_scholarly_capabilities(registry):
    import ore_scholarly  # optional plugin remains optional for a core-only install
    from .capabilities import ToolSpec, object_schema, STRING
    async def inventory(args, runtime):
        return seal_archive(registry.engine.coverage, runtime.job_id, args['snapshot_ids'], args['profile_id'], args['from'], args['until_exclusive'], lease=runtime.lease)
    async def inspect(args, runtime):
        from .coverage import audit_coverage
        return {'profiles': registry.engine.coverage.profiles(), 'sources': ore_scholarly.list_sources(),
                'coverage': audit_coverage(registry.engine.store, runtime.job_id, state_dir=registry.engine.settings.state_dir)}
    registry.register(ToolSpec('scholarly.archive_inventory', 'Lock an exact issue-date inventory from complete captured official archives using an installed trusted html_archive profile. Never use model-generated issue counts.',
        object_schema({'snapshot_ids': {'type': 'array', 'items': STRING, 'minItems': 1}, 'profile_id': STRING,
                       'from': {'type': 'string', 'format': 'date'}, 'until_exclusive': {'type': 'string', 'format': 'date'}},
                      ['snapshot_ids', 'profile_id', 'from', 'until_exclusive']), replay_safe=True), inventory)
    registry.register(ToolSpec('scholarly.inspect', 'Inspect installed source/coverage profiles and independently verified scholarly coverage.', object_schema(), read_only=True, replay_safe=True), inspect)
