#!/usr/bin/env python3
"""Run a whole-issue mission against an independently reviewed hidden manifest.

The expected file stays in this process and is never included in a model prompt or
sent to the coordinator. Use --job-id to verify an existing/resumed run. The file
must contain issue_urls, reviewer, evidence, and records with article_key,
classification and assets (role, declared_url, optional sha256).
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import httpx
import yaml


def check_expected(expected):
    if not expected.get('reviewer') or not expected.get('evidence'):
        raise ValueError('Expected manifest requires an independent reviewer and source evidence')
    if not expected.get('issue_urls') or not expected.get('records'):
        raise ValueError('Expected issue and record lists must be nonempty')
    keys = [row['article_key'] for row in expected['records']]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate expected article identity')
    for row in expected['records']:
        if row['classification'] not in ('included', 'excluded'):
            raise ValueError('Expected classifications must be independently resolved')
        if row['classification'] == 'included' and not any(a['role'] == 'main_pdf' for a in row.get('assets', [])):
            raise ValueError('Every expected included article requires a main PDF')


async def verify(client, job_id, expected):
    response = await client.get(f'/v1/jobs/{job_id}'); response.raise_for_status(); job = response.json()
    response = await client.get(f'/v1/jobs/{job_id}/coverage'); response.raise_for_status(); coverage = response.json()
    response = await client.get(f'/v1/jobs/{job_id}/artifacts'); response.raise_for_status(); artifacts = {a['id']: a for a in response.json()}
    actual = {row['article_key']: row for issue in coverage['issues'] for row in issue.get('entries', [])}
    articles = {row['article_key']: row for row in coverage['articles']}
    bindings = {row['requirement_id']: row for row in coverage['bindings']}
    wanted = {row['article_key']: row for row in expected['records']}
    gaps = []
    if set(actual) != set(wanted):
        gaps.append({'kind': 'toc_mismatch', 'missing': sorted(set(wanted)-set(actual)), 'unexpected': sorted(set(actual)-set(wanted))})
    verified = []
    for key, row in wanted.items():
        observed = articles.get(key, actual.get(key, {}))
        if observed.get('classification') != row['classification']:
            gaps.append({'kind': 'classification_mismatch', 'article_key': key})
        if row['classification'] == 'excluded':
            continue
        requirements = observed.get('requirements', [])
        expected_assets = {(a['role'], a['declared_url']): a for a in row['assets']}
        found = {(r['role'], r['declared_url']): r for r in requirements}
        if set(found) != set(expected_assets):
            gaps.append({'kind': 'asset_manifest_mismatch', 'article_key': key})
        for identity, requirement in found.items():
            binding = bindings.get(requirement['id'], {})
            artifact = artifacts.get(binding.get('artifact_id'))
            if not artifact:
                gaps.append({'kind': 'missing_file', 'article_key': key, 'requirement_id': requirement['id']}); continue
            response = await client.get(f"/v1/jobs/{job_id}/artifacts/{artifact['id']}/file")
            if response.status_code != 200:
                gaps.append({'kind': 'unreadable_file', 'artifact_id': artifact['id']}); continue
            content = response.content; digest = hashlib.sha256(content).hexdigest()
            if digest != artifact.get('sha256') or digest != binding.get('sha256'):
                gaps.append({'kind': 'hash_mismatch', 'artifact_id': artifact['id']})
            expected_hash = expected_assets.get(identity, {}).get('sha256')
            if expected_hash and digest != expected_hash:
                gaps.append({'kind': 'reference_hash_mismatch', 'artifact_id': artifact['id']})
            if identity[0] == 'main_pdf' and (not content.startswith(b'%PDF-') or binding.get('version') != 'published_version'):
                gaps.append({'kind': 'main_format_or_version_mismatch', 'artifact_id': artifact['id']})
            verified.append({'article_key': key, 'role': identity[0], 'artifact_id': artifact['id'], 'sha256': digest, 'bytes': len(content)})
    audit = job.get('audit', {})
    return {'passed': job['status'] == 'completed' and audit.get('status') == 'complete_within_scope' and not gaps,
            'job_id': job_id, 'status': job['status'], 'audit': audit, 'gaps': gaps, 'verified_files': verified,
            'expected_manifest_hidden_from_agent': True, 'expected_manifest_sha256': hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()}


async def main(args):
    expected = json.loads(args.expected.read_text()); check_expected(expected)
    token = os.environ.get('ORE_AUTH_TOKEN') or args.token_file.read_text().strip()
    if urlsplit(args.server).scheme not in ('http', 'https'):
        raise ValueError('Coordinator URL must use HTTP(S)')
    async with httpx.AsyncClient(base_url=args.server, headers={'Authorization': 'Bearer '+token}, timeout=120) as client:
        job_id = args.job_id
        if not job_id:
            if not args.mission: raise ValueError('--mission is required when creating a run')
            mission = yaml.safe_load(args.mission.read_text())
            targets = mission.get('issue_urls') or mission.get('scope', {}).get('issue_urls')
            if set(targets or []) != set(expected['issue_urls']): raise ValueError('Mission scope must match the independent issue scope')
            mission['completeness'] = 'systematic'
            mission['routing'] = {'mode': 'fixed', 'model': 'gpt-6-astra', 'effort': 'high'}
            response = await client.post('/v1/jobs', json=mission); response.raise_for_status(); job_id = response.json()['id']
            response = await client.post(f'/v1/jobs/{job_id}/run', json={}); response.raise_for_status()
            print(json.dumps({'job_id': job_id, 'status': 'started'}), flush=True)
        deadline = time.monotonic()+args.timeout
        while time.monotonic() < deadline:
            response = await client.get(f'/v1/jobs/{job_id}'); response.raise_for_status(); job = response.json()
            if job['status'] not in ('queued', 'running', 'resuming'): break
            await asyncio.sleep(2)
        report = await verify(client, job_id, expected)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps({'passed': report['passed'], 'job_id': job_id, 'status': report['status'], 'gaps': len(report['gaps']), 'report': str(args.report)}))
        return 0 if report['passed'] else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8765')
    parser.add_argument('--token-file', type=Path, default=Path('.ore/operator.token'))
    parser.add_argument('--mission', type=Path)
    parser.add_argument('--expected', type=Path, required=True)
    parser.add_argument('--job-id')
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--report', type=Path, default=Path('.ore/reports/issue-acceptance.json'))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
