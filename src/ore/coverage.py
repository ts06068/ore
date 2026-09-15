"""Immutable source snapshots and coordinator-derived scholarly coverage.

Capture/register/attempt APIs are executor/operator APIs, never model assertions.
Models may request sealing by IDs of captured evidence and trusted profiles only.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
import tempfile
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
from bs4 import BeautifulSoup
from .models import canonical_digest
def normalize_article_version(value):
    """Core normalization has no dependency on the optional scholarly package."""
    key = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    return {'published':'published_version','publishedversion':'published_version','published_version':'published_version',
            'version_of_record':'published_version','vor':'published_version','accepted':'accepted_manuscript',
            'acceptedversion':'accepted_manuscript','accepted_manuscript':'accepted_manuscript','author_manuscript':'accepted_manuscript',
            'submitted':'submitted_manuscript','submittedversion':'submitted_manuscript','submitted_manuscript':'submitted_manuscript',
            'preprint':'submitted_manuscript'}.get(key, 'unknown')

SCHEMA = 'ore.coverage/v1'
TIERS = ('journal','publisher','pmc')
BLOCKERS = ('Just a moment...', 'Verify you are human', 'Access Denied', 'Checking your browser')

class CoverageError(ValueError):
    pass


def canonical_url(url):
    part = urlsplit(str(url))
    if part.scheme not in ('http','https') or not part.hostname or part.username or part.password:
        raise CoverageError('Evidence requires a public HTTP(S) URL without embedded credentials')
    return urlunsplit((part.scheme.lower(), part.netloc.lower(), part.path or '/', part.query, ''))


def article_identity(url):
    found = re.search(r'10\.\d{4,9}/[^\s?#]+', unquote(str(url)), re.I)
    return 'doi:' + found.group().rstrip('/').lower() if found else canonical_url(url)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _text(node):
    return node.get('content') or node.get('data-article-type') or node.get_text(' ', strip=True) if node else ''


def _value(node):
    return node.get('content') or node.get('href') if node else None


def _classify(label, profile, title=""):
    # Explicit reviewed all-type profiles include every extracted article row.
    # Research-only labels/title heuristics must not silently narrow that scope.
    if profile.get('article_types') == 'all':return 'included'
    if any(re.search(pattern,title,re.I) for pattern in profile.get('ambiguous_title_patterns',[])):return 'needs_review'
    key = (label or '').strip().casefold()
    if key in {x.casefold() for x in profile.get('include_labels', [])}:return 'included'
    if key in {x.casefold() for x in profile.get('exclude_labels', [])}:return 'excluded'
    return 'needs_review'


class CoverageLedger:
    def __init__(self, store, state_dir):
        self.store, self.root = store, Path(state_dir).resolve() / 'evidence'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            from ore_scholarly.coverage_profiles import bundled_profiles
            self.bundled = {p['id']:p for p in bundled_profiles()}
        except ImportError:
            self.bundled = {}

    def _job(self, job_id):
        job = self.store.get_job(job_id)
        if not job:raise CoverageError('Unknown coverage job')
        return job

    def _key(self, job_id, kind, ident):
        job = self._job(job_id)
        return [job_id, job['revision'], job['generation'], kind, ident]

    def _put(self, job_id, kind, ident, value, lease=None):
        job = self._job(job_id)
        value = {**value, 'id':value.get('id', str(ident)), 'schema_version':SCHEMA, 'job_id':job_id, 'revision':job['revision'], 'generation':job['generation'], 'protocol_digest':job['protocol_digest']}
        value['seal_digest'] = canonical_digest(value)
        key = self._key(job_id, kind, ident)
        old = self.store.get_document('coverage.'+kind, key)
        if old:
            if old.get('seal_digest') != value['seal_digest']:raise CoverageError('Sealed evidence is immutable; create a new revision')
            return old
        from .store import DocumentConflict
        try:
            return self.store.put_document('coverage.'+kind, key, value, job_id=job_id, lease=lease, expected_version=0)
        except DocumentConflict:
            other=self.store.get_document('coverage.'+kind,key)
            if other and other.get('seal_digest')==value['seal_digest']:return self._get(job_id,kind,ident)
            raise CoverageError('Concurrent sealed evidence conflicts; create a new revision')

    def _get(self, job_id, kind, ident):
        value = self.store.get_document('coverage.'+kind, self._key(job_id, kind, ident))
        if not value:raise CoverageError('Required sealed '+kind+' evidence is missing')
        payload = {k:v for k,v in value.items() if k not in ('seal_digest','state_version','created_at','updated_at')}
        if canonical_digest(payload) != value.get('seal_digest'):raise CoverageError('Sealed evidence digest mismatch')
        return value

    def _current(self, job_id, kind):
        job = self._job(job_id)
        return [r for r in self.store.list_documents('coverage.' + kind, job_id=job_id)
                if r.get('revision') == job['revision'] and r.get('generation') == job['generation']
                and r.get('protocol_digest') == job['protocol_digest']]

    def profiles(self):
        custom = self.store.list_documents('coverage.profile')
        return list(self.bundled.values()) + custom

    def register_profile(self, profile, *, reviewer):
        if not reviewer or not isinstance(profile, dict) or not profile.get('id') or not profile.get('origins'):
            raise CoverageError('Trusted profile requires reviewer, ID and authoritative origins')
        if profile.get('kind') not in ('html_issue','html_article','jats_article','html_archive'):raise CoverageError('Unsupported extraction profile')
        value = {**profile, 'reviewer':reviewer}
        value['profile_digest'] = canonical_digest(value)
        old = self.store.get_document('coverage.profile', profile['id'])
        if old:
            if old.get('profile_digest') != value['profile_digest']:raise CoverageError('Extractor profiles are immutable; use a new ID')
            return old
        return self.store.put_document('coverage.profile', profile['id'], value, expected_version=0)

    def _profile(self, ident, kind):
        profile = self.store.get_document('coverage.profile', ident) or self.bundled.get(ident)
        if not profile or profile['kind'] not in kind:raise CoverageError('Trusted profile of the required kind is missing')
        return profile

    def _check_article_type_scope(self, job_id, profile, classifications=()):
        if (self._job(job_id)['mission'].get('scope') or {}).get('article_types') != 'all':
            return
        if profile.get('article_types') != 'all':
            raise CoverageError("All-article scope requires a reviewed extraction profile with article_types='all'")
        if any(value != 'included' for value in classifications):
            raise CoverageError('All-article scope cannot exclude or leave unclassified extracted article rows')

    def capture_snapshot(self, job_id, *, url, content, media_type='text/html', authority=None, source_id=None, status_code=200, capture_kind='response_body', lease=None):
        if not isinstance(content, bytes) or len(content)>32*1024*1024:raise CoverageError('Snapshot must be complete executor-captured bytes up to 32 MiB')
        authority = authority or ('pmc' if source_id == 'pmc' else 'journal')
        if authority not in TIERS:raise CoverageError('Unknown authority tier')
        original_digest = _sha(content)
        sanitization = []
        if 'html' in media_type:
            document = BeautifulSoup(content, 'html.parser')
            if document.select('input[type="password"]'):
                raise CoverageError('Authentication forms cannot be retained as scholarly evidence')
            for node in document.select('input[value], button[value], option[value]'):
                del node['value'];sanitization.append('form_values_removed')
            for node in document.select('textarea'):
                node.clear();sanitization.append('form_values_removed')
            for node in document.select('script, meta[name*="csrf" i], meta[name*="token" i]'):
                node.decompose();sanitization.append('scripts_and_token_metadata_removed')
            if sanitization:
                content = str(document).encode('utf-8')
                capture_kind += '_sanitized'
        digest = _sha(content);ident = canonical_digest([canonical_url(url),digest,status_code,capture_kind,authority])
        path = self.root / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():raise CoverageError('Snapshot path cannot be a symbolic link')
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
                temporary=Path(stream.name);stream.write(content);stream.flush();os.fsync(stream.fileno())
            try:
                try:os.link(temporary,path)
                except FileExistsError:pass
            finally:temporary.unlink(missing_ok=True)
        if path.is_symlink() or _sha(path.read_bytes()) != digest:raise CoverageError('Stored snapshot bytes are corrupt')
        return self._put(job_id,'snapshot',ident,{'id':ident,'url':canonical_url(url),'sha256':digest,'path':str(path),'bytes':len(content),
            'media_type':media_type,'original_sha256':original_digest,'sanitization':sorted(set(sanitization)),'authority':authority,'source_id':source_id,'status_code':status_code,'capture_kind':capture_kind},lease)

    def _snapshots(self, job_id, ids, profile=None):
        if not ids or len(ids)!=len(set(ids)):raise CoverageError('Distinct captured snapshot IDs are required')
        values=[]
        for ident in ids:
            item=self._get(job_id,'snapshot',ident);path=Path(item['path'])
            if not path.resolve().is_relative_to(self.root) or path.is_symlink() or _sha(path.read_bytes())!=item['sha256']:
                raise CoverageError('Snapshot original bytes are unavailable or changed')
            if profile and urlsplit(item['url']).hostname not in profile['origins']:raise CoverageError('Snapshot is outside trusted profile origins')
            if item['status_code']!=200:raise CoverageError('Non-success response cannot establish a manifest')
            data=path.read_bytes()
            if any(text.encode() in data for text in BLOCKERS):raise CoverageError('Challenge/error page cannot establish coverage')
            values.append((item,data))
        return values

    def declare_collection(self, job_id, issue_ids, *, evidence_snapshot_ids=(), reviewer='mission_scope', selection_rule=None, lease=None):
        urls=sorted(set(canonical_url(u) for u in issue_ids))
        if not urls:raise CoverageError('An explicit finite issue target is required')
        if reviewer!='mission_scope' and not evidence_snapshot_ids:raise CoverageError('Archive-derived targets require independently captured inventory evidence')
        if evidence_snapshot_ids:self._snapshots(job_id,list(evidence_snapshot_ids))
        # Archive discovery must use a reviewed complete archive extractor; this API
        # only seals explicit operator/user targets and never trusts LLM counts.
        if reviewer!='mission_scope':raise CoverageError('Archive-derived collection sealing requires a reviewed archive manifest; unsupported here')
        return self._put(job_id,'collection','target',{'issue_ids':urls,'evidence_snapshot_ids':list(evidence_snapshot_ids),
            'selection_rule':selection_rule,'target_basis':'explicit_mission_scope','reviewer':reviewer},lease)

    def seal_issue(self, job_id, issue_id, snapshot_ids, profile_id, *, lease=None):
        issue_id=canonical_url(issue_id);target=self._get(job_id,'collection','target')
        if issue_id not in target['issue_ids']:raise CoverageError('Issue is outside the locked collection target')
        profile=self._profile(profile_id,('html_issue',));self._check_article_type_scope(job_id,profile)
        captured=self._snapshots(job_id,snapshot_ids,profile)
        if issue_id not in {s['url'] for s,_ in captured}:raise CoverageError('The target issue URL must be captured')
        entries={};next_urls=set();observed={s['url'] for s,_ in captured};reported=[]
        for snapshot,data in captured:
            soup=BeautifulSoup(data,'html.parser')
            container=soup.select_one(profile['container_selector'])
            if container is None or not soup.select(profile['complete_selector']):raise CoverageError('Issue template/complete marker is not recognized')
            if profile.get('pending_selector') and soup.select(profile['pending_selector']):raise CoverageError('Issue still contains pending/expandable content')
            for node in soup.select(profile.get('next_selector','a[rel="next"]')):
                if node.get('href'):next_urls.add(canonical_url(urljoin(snapshot['url'],node['href'])))
            count=soup.select_one(profile.get('count_selector','[data-total-articles]'))
            if count:
                value=count.get(profile.get('count_attribute','data-total-articles')) or count.get_text(strip=True)
                if not re.fullmatch(r'\d+',str(value)):raise CoverageError('Publisher issue count is not an integer')
                reported.append(int(value))
            seen_links=set()
            for row in container.select(profile['row_selector']):
                link=row.select_one(profile['link_selector'])
                if not link or not link.get('href'):raise CoverageError('A TOC row has no authoritative identity link')
                url=canonical_url(urljoin(snapshot['url'],link['href']));key=article_identity(url);seen_links.add(key)
                label=_text(row.select_one(profile.get('article_type_selector','.article-type')))
                record={'article_key':key,'url':url,'title':link.get_text(' ',strip=True),'article_type_raw':label,
                        'classification':_classify(label,profile,link.get_text(' ',strip=True)),'snapshot_id':snapshot['id']}
                if key in entries and (entries[key]['url']!=url or entries[key]['classification']!=record['classification']):raise CoverageError('Conflicting duplicate TOC identity')
                entries[key]=record
            if not seen_links:raise CoverageError('No issue records were observed')
            pattern=profile.get('article_url_pattern')
            if pattern:
                all_keys={article_identity(urljoin(snapshot['url'],a['href'])) for a in container.select('a[href]') if re.search(pattern,a['href'])}
                if all_keys-seen_links:raise CoverageError('Unaccounted article links remain outside extracted TOC rows')
        if next_urls-observed:raise CoverageError('Issue pagination is not exhausted')
        if reported and any(total!=len(entries) for total in reported):raise CoverageError('Extracted identities disagree with publisher count')
        value={'issue_id':issue_id,'journal_id':profile.get('journal_id'),'profile_id':profile_id,'profile_digest':canonical_digest(profile),'extractor_profile':profile,
               'snapshot_ids':snapshot_ids,'entries':sorted(entries.values(),key=lambda r:r['article_key']),'expected_resources':len(entries),'pagination_complete':True}
        return self._put(job_id,'issue',issue_id,value,lease)

    def seal_article(self, job_id, article_key, snapshot_ids, profile_id, *, lease=None):
        profile=self._profile(profile_id,('html_article','jats_article'));self._check_article_type_scope(job_id,profile)
        captured=self._snapshots(job_id,snapshot_ids,profile)
        issues=[issue for issue in self._current(job_id,'issue') if any(r['article_key']==article_key for r in issue.get('entries',[]))]
        for issue in issues:
            self._check_article_type_scope(job_id,issue.get('extractor_profile') or {},[r['classification'] for r in issue['entries']])
        issue_rows=[r for issue in issues for r in issue['entries'] if r['article_key']==article_key]
        if not issue_rows:raise CoverageError('Article is absent from sealed issue inventory')
        matches=[(snap,data) for snap,data in captured if article_identity(snap['url'])==article_key or snap['url']==issue_rows[0]['url']]
        if not matches and not article_key.startswith('doi:'):raise CoverageError('Article snapshots do not match the TOC identity')
        assets={};labels=set();absence=False;identity=False;listing_complete=False;main_alternatives=[];reported_supp_counts=[];titles={r.get("title","") for r in issue_rows}
        for snap,data in captured:
            soup=BeautifulSoup(data,'html.parser')
            titles.update(_text(node) for node in soup.select('meta[name="citation_title"], h1, article-title'))
            meta=soup.select_one('meta[name="citation_doi"]');doi=(_text(meta) or '').removeprefix('https://doi.org/').lower()
            if profile['kind']=='jats_article':
                try:
                    from defusedxml import ElementTree as ET
                except ImportError as exc:
                    raise CoverageError('JATS extraction requires the optional scholarly package') from exc
                root=ET.fromstring(data)
                for node in root.iter():node.tag=node.tag.rsplit('}',1)[-1]
                doi=next((''.join(n.itertext()).strip().lower() for n in root.findall('.//article-id') if n.get('pub-id-type')=='doi'),'')
                label=root.get('article-type','');labels.add(label)
                identity |= article_key=='doi:'+doi
                for node in root.findall('.//self-uri'):
                    href=node.get('{http://www.w3.org/1999/xlink}href') or node.get('href')
                    if href and (node.get('content-type')=='pdf' or href.lower().endswith('.pdf')):assets[urljoin(snap['url'],href)]=('main_pdf',snap['id'])
                supp=root.findall('.//supplementary-material')+root.findall('.//inline-supplementary-material')
                for node in supp:
                    links=[n.get('{http://www.w3.org/1999/xlink}href') or n.get('href') for n in node.iter()]
                    links=[h for h in links if h]
                    if not links:raise CoverageError('Declared JATS supplement has no retrievable link')
                    for href in links:assets[urljoin(snap['url'],href)]=('supplement',snap['id'])
                if root.find('.//body') is None:raise CoverageError('Complete publisher JATS body is required')
                listing_complete=True
                absence |= not supp
            else:
                if profile.get('pending_selector') and soup.select(profile['pending_selector']):raise CoverageError('Article is pending or access-limited')
                if article_key.startswith('doi:') and doi and article_key!='doi:'+doi:raise CoverageError('Article DOI contradicts its issue identity')
                identity |= article_key=='doi:'+doi or snap['url']==issue_rows[0]['url']
                label=_text(soup.select_one(profile.get('article_type_selector','.article-type')))
                if label:labels.add(label)
                for role,selector in [('main_pdf',profile['main_selector']),('supplement',profile['supplement_selector'])]:
                    for node in soup.select(selector):
                        href=_value(node)
                        if href:
                            asset_url=canonical_url(urljoin(snap['url'],href))
                            if asset_url in assets and assets[asset_url][0]!=role:raise CoverageError('One asset URL has conflicting main/supplement roles')
                            assets[asset_url]=(role,snap['id'])
                pattern=profile.get('supplement_url_pattern')
                if pattern:
                    all_supp={canonical_url(urljoin(snap['url'],n['href'])) for n in soup.select('a[href]') if re.search(pattern,n['href'],re.I)}
                    extracted={u for u,(role,_) in assets.items() if role=='supplement'}
                    if all_supp-extracted:raise CoverageError('Supplement links remain outside the trusted complete listing')
                declared=soup.select_one(profile.get('supplement_count_selector','[data-supplement-count]'))
                if declared:
                    count=declared.get(profile.get('supplement_count_attribute','data-supplement-count'))
                    if not str(count).isdigit():raise CoverageError('Supplement count is not an integer')
                    if int(count)!=len([1 for r,_ in assets.values() if r=='supplement']):raise CoverageError('Supplement files disagree with publisher count')
                    reported_supp_counts.append(int(count))
                    listing_complete=True
                if profile.get('attachments_complete_selector') and soup.select(profile['attachments_complete_selector']):
                    listing_complete=True
                absence |= bool(soup.select(profile.get('empty_supplements_selector','[data-supplement-count="0"]')))
                if absence:listing_complete=True
        if not identity:raise CoverageError('Authoritative article identity was not confirmed')
        classifications={_classify(label,profile) for label in labels if label}
        if 'needs_review' in classifications:raise CoverageError('Unrecognized authoritative article-type label requires profile review')
        toc_classes={r['classification'] for r in issue_rows if r['classification']!='needs_review'}
        if len(classifications|toc_classes)>1:raise CoverageError('Publisher article classifications disagree')
        classification=next(iter(classifications|toc_classes),'needs_review')
        if classification=='needs_review':raise CoverageError('Original-article type remains unverified')
        if classification=='included' and profile.get('article_types')!='all' and any(re.search(pattern,title,re.I) for pattern in profile.get('ambiguous_title_patterns',[]) for title in titles):
            raise CoverageError('Review-like title requires an explicit reviewed eligibility rule; broad research label is insufficient')
        primary_main = None
        mains=[u for u,(role,_) in assets.items() if role=='main_pdf']
        if mains:
            # One logical main-PDF requirement; alternate official URLs are candidates.
            primary_main=min(mains,key=lambda u:('/epdf/' in u, '/doi/pdf/' not in u, u))
            main_alternatives=[{'url':u,'snapshot_id':assets[u][1]} for u in mains if u!=primary_main]
            assets={u:entry for u,entry in assets.items() if entry[0]!='main_pdf' or u==primary_main}
        requirements=[]
        for url,(role,snapshot_id) in sorted(assets.items()):
            url=canonical_url(url);rid=canonical_digest([article_key,role,url])
            requirements.append({'id':rid,'article_key':article_key,'role':role,'declared_url':url,'snapshot_id':snapshot_id,
                'authority':'publisher' if urlsplit(url).hostname in profile.get('publisher_origins',[]) else profile.get('authority','journal'),
                'covered_authorities':['journal','publisher'] if profile.get('journal_is_publisher') else ['publisher'] if urlsplit(url).hostname in profile.get('publisher_origins',[]) else [profile.get('authority','journal')],
                'version':'published_version' if role=='main_pdf' and profile.get('final_version_from_official_pdf_link') else 'unknown'})
        main=[r for r in requirements if r['role']=='main_pdf'];supp=[r for r in requirements if r['role']=='supplement']
        if any(count!=len(supp) for count in reported_supp_counts) or (absence and supp):
            raise CoverageError('Captured supplement declarations contradict each other')
        if classification=='included' and not main:raise CoverageError('Included article has no declared main PDF')
        if classification=='included' and not listing_complete:raise CoverageError('The authoritative attachment listing is not demonstrably complete')
        if classification=='included' and not supp and not absence:raise CoverageError('Supplement absence is not established by a complete authoritative listing')
        value={'article_key':article_key,'classification':classification,'article_types_raw':sorted(labels),'profile_id':profile_id,'profile_digest':canonical_digest(profile),'extractor_profile':profile,'attachment_listing_complete':listing_complete,'main_alternatives':main_alternatives,
            'snapshot_ids':snapshot_ids,'requirements':requirements if classification=='included' else [],
            'supplement_status':'present' if supp else 'verified_empty_manifest' if absence else 'not_required'}
        sealed=self._put(job_id,'article',article_key,value,lease)
        if classification=='included':
            self.register_candidates(job_id,article_key,[{'url':r['declared_url'],'role':r['role'],'version':r['version'],'requirement_id':r['id']} for r in requirements],source=profile.get('authority','journal'),snapshot_ids=snapshot_ids,lease=lease)
        return {**sealed,'candidates':self.candidates(job_id,article_key)}

    def candidates(self, job_id, article_key=None):
        return [c for c in self._current(job_id,'candidate') if article_key is None or c.get('article_key')==article_key]

    def register_candidates(self, job_id, article_key, candidates, *, source, snapshot_ids, lease=None):
        article=self._get(job_id,'article',article_key);snapshots=self._snapshots(job_id,snapshot_ids)
        tier='pmc' if source=='pmc' else source
        if tier not in TIERS:raise CoverageError('Unknown candidate authority; publisher/journal role must come from trusted extractor')
        if any(s['authority']!=tier for s,_ in snapshots):raise CoverageError('Candidate authority disagrees with captured evidence')
        result=[]
        for candidate in candidates:
            role=candidate.get('role')
            if role not in ('main_pdf','supplement'):continue
            url=canonical_url(candidate['url'])
            matching=[r for r in article['requirements'] if r['role']==role and (r['declared_url']==url or r['id']==candidate.get('requirement_id'))]
            if role=='main_pdf' and not matching:matching=[r for r in article['requirements'] if r['role']=='main_pdf']
            if len(matching)!=1:raise CoverageError('Candidate must match one explicit article artifact requirement')
            # Resolver output is captured by the executor, not supplied by a model.
            def contains_url(snap,body):
                if url.encode() in body or unquote(url).encode() in body or snap['url']==url:return True
                if 'html' in snap['media_type']:
                    document=BeautifulSoup(body,'html.parser')
                    for node in document.select('[href], meta[name="citation_pdf_url"]'):
                        href=_value(node)
                        if not href:continue
                        try:
                            if canonical_url(urljoin(snap['url'],href))==url:return True
                        except CoverageError:continue
                    return False
                return False
            if not any(contains_url(snap,body) for snap,body in snapshots):
                raise CoverageError('Candidate URL is absent from captured source evidence')
            if candidate.get('doi') and article_key.startswith('doi:') and str(candidate['doi']).lower().removeprefix('https://doi.org/')!=article_key[4:]:
                raise CoverageError('Resolver candidate DOI differs from the sealed article')
            version=normalize_article_version(candidate.get('version'))
            official=matching[0]['declared_url']==url and any(s['id']==matching[0]['snapshot_id'] for s,_ in snapshots)
            candidate_tier=matching[0].get('authority',tier) if official else tier
            covered=matching[0].get('covered_authorities',[candidate_tier]) if official else [candidate_tier]
            cid=canonical_digest([article_key,matching[0]['id'],url,candidate_tier,version])
            result.append(self._put(job_id,'candidate',cid,{'id':cid,'article_key':article_key,'requirement_id':matching[0]['id'],'url':url,'role':role,
                'authority':candidate_tier,'covered_authorities':covered,'version':version,'version_raw':candidate.get('version'),'snapshot_ids':snapshot_ids},lease))
        return result

    def record_attempt(self, job_id, article_key, tier, outcome, *, snapshot_ids=(), requirement_id=None, source_url=None, lease=None):
        if tier not in TIERS or outcome not in ('unavailable','not_found','access_required','challenge','temporarily_unavailable','no_eligible_copy','success'):
            raise CoverageError('Invalid executor retrieval outcome')
        if not source_url and not snapshot_ids:raise CoverageError('Executor attempt needs source URL or captured response evidence')
        ident=canonical_digest([article_key,tier,outcome,list(snapshot_ids),requirement_id,source_url])
        return self._put(job_id,'attempt',ident,{'id':ident,'article_key':article_key,'authority':tier,'outcome':outcome,
            'snapshot_ids':list(snapshot_ids),'requirement_id':requirement_id,'source_url':canonical_url(source_url) if source_url else None},lease)

    def check_download(self, job_id, candidate_id, requirement_id, *, url=None, resource_id=None, role=None):
        candidate=self._get(job_id,'candidate',candidate_id)
        if candidate['requirement_id']!=requirement_id:raise CoverageError('Candidate belongs to a different artifact requirement')
        if candidate['role']=='main_pdf' and candidate['version']!='published_version':raise CoverageError('Only evidenced published final versions satisfy this collection')
        if url is not None and canonical_url(url)!=candidate['url']:raise CoverageError('Download URL differs from sealed candidate')
        if role is not None and role!=candidate['role']:raise CoverageError('Download role differs from sealed requirement')
        if resource_id is not None:self._match_resource(job_id,resource_id,candidate['article_key'])
        policy=self._job(job_id)['mission'].get('retrieval_policy') or {}
        api_first=isinstance(policy,dict) and policy.get('mode')=='api_open_access_first'
        # Transport preference never changes sealed official obligations, identity,
        # main publication version or supplement matching. Only the requirement
        # to exhaust higher delivery tiers is optional under explicit API/OA-first.
        if not api_first:
            attempts=[self._get(job_id,'attempt',a['id']) for a in self._current(job_id,'attempt')]
            for tier in TIERS[:TIERS.index(candidate['authority'])]:
                if not any(a['article_key']==candidate['article_key'] and a['authority']==tier and a['outcome']!='success'
                           and a.get('requirement_id') in (None,requirement_id) for a in attempts):
                    raise CoverageError('Higher-authority source must have an executor-recorded unavailable outcome first: '+tier)
        return candidate

    def _match_resource(self, job_id, resource_id, article_key):
        resource=next((r for r in self.store.resources(job_id) if r['id']==resource_id),None)
        keys={str(resource.get(k)).lower() for k in ('resource_key','canonical_id','doi','url','id')} if resource else set()
        if article_key.lower() not in keys and article_key.removeprefix('doi:').lower() not in keys:
            raise CoverageError('Artifact is attached to a different article')
        return resource

    def bind_artifact(self, job_id, requirement_id, candidate_id, artifact, *, lease=None):
        candidate=self.check_download(job_id,candidate_id,requirement_id)
        if artifact.get('status')!='verified' or artifact.get('integrity')!='verified':raise CoverageError('Artifact bytes have not been verified')
        if artifact.get('role')!=candidate['role'] or (candidate['role']=='main_pdf' and normalize_article_version(artifact.get('version'))!='published_version'):raise CoverageError('Artifact role/version does not fulfill requirement')
        actual=canonical_url(artifact.get('source_url') or artifact.get('url'))
        requested=canonical_url(artifact.get('requested_source_url') or actual)
        chain=[canonical_url(u) for u in artifact.get('redirect_chain', [actual])]
        if requested!=candidate['url'] or not chain or chain[0]!=requested or chain[-1]!=actual:
            raise CoverageError('Artifact request and executor redirect evidence differ from authorized candidate')
        path=Path(artifact['path']);vault=self.root.parent/'vault'
        if not path.resolve().is_relative_to(vault) or path.is_symlink() or _sha(path.read_bytes())!=artifact.get('sha256'):raise CoverageError('Original artifact bytes fail independent verification')
        self._match_resource(job_id,artifact.get('resource_id'),candidate['article_key'])
        return self._put(job_id,'binding',requirement_id,{'requirement_id':requirement_id,'candidate_id':candidate_id,'article_key':candidate['article_key'],
            'artifact_id':artifact['id'],'sha256':artifact['sha256'],'path':str(path),'role':candidate['role'],'version':candidate['version']},lease)


def audit_coverage(store, job_id, *, state_dir):
    job=store.get_job(job_id);mission=job['mission'];strict=mission.get('completeness') in ('inventory','systematic') or (mission.get('scope') or {}).get('coverage_schema')==SCHEMA
    legacy = mission.get('audit_contract')=='legacy_bounded' or job.get('audit_contract')=='legacy_bounded'
    if legacy:strict=False
    if not strict:return {'applicable':False,'status':'legacy_bounded' if legacy else 'bounded','inventory_verified':False,'coverage_denominator':None,'gaps':[],'counts':{}}
    from .source_policy import audit_required_operations
    ledger=CoverageLedger(store,state_dir);gaps=audit_required_operations(store,job_id);counts={'issues_expected':0,'issues_verified':0,'resources_expected':0,'included':0,'excluded':0,'classification_unresolved':0,'main_expected':0,'supplements_expected':0,'artifacts_verified':0,'assets_unavailable':0,'toc_included':0,'toc_excluded':0,'toc_unresolved':0,
        'resources_complete':0,'main_verified':0,'supplements_verified':0,'supplements_none_confirmed':0,'supplement_inventory_unknown':0,'included_with_supplements':0}
    try:target=ledger._get(job_id,'collection','target')
    except CoverageError as exc:return {'applicable':True,'status':'incomplete','inventory_verified':False,'coverage_denominator':None,'gaps':gaps+[{'kind':'collection_manifest_missing','message':str(exc)}],'counts':counts}
    counts['issues_expected']=len(target['issue_ids']);entries={};verified_issues=[];complete_keys=set()
    for issue_id in target['issue_ids']:
        try:
            issue=ledger._get(job_id,'issue',issue_id);ledger._snapshots(job_id,issue['snapshot_ids'])
            ledger._check_article_type_scope(job_id,issue.get('extractor_profile') or {},[entry['classification'] for entry in issue['entries']])
            counts['issues_verified']+=1;verified_issues.append(issue)
            for entry in issue['entries']:
                if entry['article_key'] in entries:gaps.append({'kind':'duplicate_issue_article','article_key':entry['article_key']})
                entries[entry['article_key']]=entry
        except (CoverageError,OSError) as exc:gaps.append({'kind':'issue_manifest_unverified','issue_id':issue_id,'message':str(exc)})
    counts['resources_expected']=len(entries)
    for entry in entries.values():
        counts['toc_'+entry['classification'] if entry['classification'] in ('included','excluded') else 'toc_unresolved']+=1
    for key,entry in entries.items():
        try:
            article=ledger._get(job_id,'article',key);ledger._snapshots(job_id,article['snapshot_ids'])
            ledger._check_article_type_scope(job_id,article.get('extractor_profile') or {},[article['classification']])
        except (CoverageError,OSError) as exc:
            # Even exclusion must be supported by the independently parsed TOC.
            if entry['classification']=='excluded':counts['excluded']+=1;continue
            counts['classification_unresolved']+=1
            counts['supplement_inventory_unknown']+=int(entry['classification']=='included')
            gaps.append({'kind':'article_manifest_missing','article_key':key,'message':str(exc)});continue
        counts[article['classification']]+=1
        if article['classification']=='excluded':continue
        article_complete=True
        counts['supplements_none_confirmed']+=int(article.get('supplement_status')=='verified_empty_manifest')
        counts['included_with_supplements']+=int(article.get('supplement_status')=='present')
        counts['supplement_inventory_unknown']+=int(article.get('supplement_status') not in ('present','verified_empty_manifest'))
        for requirement in article['requirements']:
            counts['main_expected' if requirement['role']=='main_pdf' else 'supplements_expected']+=1
            try:
                binding=ledger._get(job_id,'binding',requirement['id']);ledger.check_download(job_id,binding['candidate_id'],requirement['id'])
                if _sha(Path(binding['path']).read_bytes())!=binding['sha256']:raise CoverageError('Artifact hash changed')
                artifact=next((a for a in store.artifacts(job_id) if a['id']==binding['artifact_id']),None)
                if not artifact or artifact.get('status')!='verified' or artifact.get('integrity')!='verified' or artifact.get('sha256')!=binding['sha256'] or artifact.get('role')!=requirement['role']:raise CoverageError('Bound artifact manifest is missing or changed')
                if requirement['role']=='main_pdf' and normalize_article_version(artifact.get('version'))!='published_version':raise CoverageError('Main artifact publication version changed')
                counts['artifacts_verified']+=1
                counts['main_verified' if requirement['role']=='main_pdf' else 'supplements_verified']+=1
            except (CoverageError,OSError) as exc:
                article_complete=False
                unavailable=any(a.get('article_key')==key and a.get('requirement_id') in (None,requirement['id']) and a.get('outcome')!='success' for a in ledger._current(job_id,'attempt'))
                counts['assets_unavailable']+=int(unavailable)
                gaps.append({'kind':'required_asset_unavailable' if unavailable else 'required_asset_unverified','article_key':key,'requirement_id':requirement['id'],'message':str(exc)})
        if article_complete and article.get('attachment_listing_complete') is True:
            counts['resources_complete']+=1;complete_keys.add(key)
    counts['issues_complete']=sum(all(entry['classification']=='excluded' or entry['article_key'] in complete_keys for entry in issue['entries']) for issue in verified_issues)
    selection=target.get('selection_rule') or (mission.get('scope') or {}).get('issue_selection') or {}
    if selection.get('mode')=='first_regular_issue_with_original_and_supplement' and (counts['included']==0 or counts['supplements_expected']==0):
        gaps.append({'kind':'issue_selection_not_satisfied','message':'Selected issue must contain original research and an explicitly declared supplement'})
    inventory_verified=counts['issues_verified']==counts['issues_expected'] and not any(g['kind'] in ('duplicate_issue_article','article_manifest_missing') for g in gaps)
    return {'applicable':True,'schema_version':SCHEMA,'status':'complete_within_scope' if not gaps else 'incomplete','inventory_verified':inventory_verified,
        'coverage_denominator':counts['resources_expected'] if counts['issues_verified']==counts['issues_expected'] else None,'counts':counts,'gaps':gaps,'global_recall':'unknown'}
