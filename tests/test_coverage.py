"""Offline adversarial coverage contracts; no source access or model assertions."""
import hashlib
from pathlib import Path

import pytest

from ore.coverage import CoverageError, CoverageLedger, audit_coverage
from ore.models import Mission
from ore.store import Store

ISSUE = 'https://journal.example/issue/2024/1'
ARTICLE = 'https://journal.example/doi/10.1234/one'
KEY = 'doi:10.1234/one'
MAIN = 'https://journal.example/paper.pdf'
SUPP = 'https://journal.example/data.csv'


@pytest.fixture
def ledger(tmp_path):
    store = Store('sqlite:///:memory:');store.initialize()
    job = store.create_job(Mission(goal='Collect complete fixture issue', completeness='systematic').model_dump(mode='json'))
    value = CoverageLedger(store, tmp_path)
    base = {'origins':['journal.example'], 'include_labels':['Original Article'], 'exclude_labels':['Editorial'], 'authority':'journal'}
    value.register_profile({**base, 'id':'fixture.issue', 'kind':'html_issue', 'container_selector':'main',
        'complete_selector':'footer[data-end]', 'row_selector':'.row', 'link_selector':'a.title',
        'article_type_selector':'.type', 'article_url_pattern':r'/doi/', 'pending_selector':'.pending'}, reviewer='offline-fixture')
    value.register_profile({**base, 'id':'fixture.article', 'kind':'html_article', 'article_type_selector':'.type',
        'main_selector':'a.main', 'supplement_selector':'.attachments a', 'attachments_complete_selector':'.attachments[data-complete]',
        'supplement_url_pattern':r'/supplement', 'empty_supplements_selector':'[data-supplement-count="0"]',
        'full_article_selector':'.references', 'final_version_from_official_pdf_link':True}, reviewer='offline-fixture')
    value.declare_collection(job['id'], [ISSUE])
    yield value, job['id'], tmp_path
    store.close()


def snap(ledger, job, url, text, **kwargs):
    return ledger.capture_snapshot(job, url=url, content=text.encode(), **kwargs)['id']


def issue_html(extra='', count=1, label='Original Article'):
    return f'<main data-total-articles="{count}"><div class="row"><span class="type">{label}</span><a class="title" href="{ARTICLE}">One</a></div>{extra}</main><footer data-end></footer>'


def article_html(extra='', supplements=True, label='Original Article'):
    listing = f'<div class="attachments" data-complete><a href="{SUPP}">Data</a></div>' if supplements else '<p data-supplement-count="0">No supplements</p>'
    return f'<meta name="citation_doi" content="10.1234/one"><span class="type">{label}</span><a class="main" href="{MAIN}">PDF</a>{listing}{extra}'


def seal(ledger, job, body=None):
    ledger.seal_issue(job, ISSUE, [snap(ledger, job, ISSUE, issue_html())], 'fixture.issue')
    return ledger.seal_article(job, KEY, [snap(ledger, job, ARTICLE, body or article_html())], 'fixture.article')


def bind(ledger, job, root, candidate, *, final=None):
    resource = ledger.store.upsert_resource(job, {'doi':'10.1234/one', 'url':ARTICLE})
    payload = b'%PDF-1.4\nfixture main bytes' if candidate['role']=='main_pdf' else b'x,y\n1,2\n'
    sha = hashlib.sha256(payload).hexdigest();path=root/'vault'/sha
    path.parent.mkdir(exist_ok=True);path.write_bytes(payload)
    record = {'resource_id':resource['id'], 'role':candidate['role'], 'version':candidate['version'],
        'status':'verified','integrity':'verified','source_url':final or candidate['url'], 'requested_source_url':candidate['url'],
        'redirect_chain':[candidate['url'],final] if final else [candidate['url']], 'sha256':sha,'path':str(path)}
    artifact = ledger.store.add_artifact(job, record)
    return ledger.bind_artifact(job,candidate['requirement_id'],candidate['id'],artifact)


def test_seals_survive_store_envelope_and_real_byte_audit(ledger):
    value,job,root=ledger;article=seal(value,job)
    initial=audit_coverage(value.store,job,state_dir=root)
    assert initial['inventory_verified'] is True and initial['coverage_denominator']==1
    assert initial['status']=='incomplete' and initial['counts']['supplements_expected']==1
    for candidate in article['candidates']:bind(value,job,root,candidate)
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='complete_within_scope' and result['counts']['artifacts_verified']==2
    assert result['global_recall']=='unknown'
    assert value._get(job,'collection','target')['issue_ids']==[ISSUE]


@pytest.mark.parametrize('body,match', [
    (issue_html(count=2),'publisher count'),
    (issue_html('<a href="/doi/10.1234/missing">Missing row</a>'),'Unaccounted'),
    (issue_html('<a rel="next" href="?page=2">Next</a>'),'pagination'),
    (issue_html('<div class="pending"></div>'),'pending'),
    (issue_html().replace('<footer data-end></footer>',''),'complete marker'),
])
def test_partial_issue_never_seals(ledger,body,match):
    value,job,_=ledger
    with pytest.raises(CoverageError,match=match):value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,body)],'fixture.issue')


@pytest.mark.parametrize('body,match', [
    ('<span class="type">Original Article</span><a class="main" href="/paper.pdf">PDF</a><div class="references">References</div>','not demonstrably complete'),
    (article_html().replace('10.1234/one','10.1234/wrong'),'DOI contradicts'),
    (article_html('<a href="/supplement-hidden.xlsx">Hidden</a>'),'outside the trusted'),
    (article_html('<div data-supplement-count="2"></div>'),'publisher count'),
    (article_html(label='Unreviewed new label'),'Unrecognized'),
    (article_html(label='Editorial'),'classifications disagree'),
])
def test_article_identity_type_and_supplement_truth(ledger,body,match):
    value,job,_=ledger
    with pytest.raises(CoverageError,match=match):seal(value,job,body)


def test_explicit_empty_is_distinct_from_unknown(ledger):
    value,job,_=ledger;article=seal(value,job,article_html(supplements=False))
    assert article['supplement_status']=='verified_empty_manifest'
    assert len(article['requirements'])==1


def test_snapshot_bytes_tamper_invalidates_coverage(ledger):
    value,job,root=ledger;seal(value,job)
    snapshot=value._current(job,'snapshot')[0]
    Path(snapshot['path']).write_bytes(b'changed')
    assert audit_coverage(value.store,job,state_dir=root)['inventory_verified'] is False


def test_snapshot_and_profile_immutable(ledger):
    value,job,_=ledger
    first=snap(value,job,ISSUE,issue_html())
    assert snap(value,job,ISSUE,issue_html())==first
    with pytest.raises(CoverageError,match='immutable'):value.declare_collection(job,['https://journal.example/other'])
    profile=value._profile('fixture.issue',('html_issue',))
    with pytest.raises(CoverageError,match='immutable'):value.register_profile({**profile,'row_selector':'.other'},reviewer='offline-fixture')


def test_login_data_are_not_retained(ledger):
    value,job,_=ledger
    capture=value.capture_snapshot(job,url=ISSUE,content=b'<input value="private-value"><textarea>private-text</textarea><script>private-script</script><meta name="csrf-token" content="private-token">')
    data=Path(capture['path']).read_text()
    assert 'private-' not in data and capture['sanitization'] and capture['sha256']!=capture['original_sha256']
    with pytest.raises(CoverageError,match='Authentication'):snap(value,job,ISSUE,'<input type="password" value="private">')


def test_wrong_url_role_resource_rejected_before_download(ledger):
    value,job,_=ledger;article=seal(value,job);candidate=article['candidates'][0]
    wrong=value.store.upsert_resource(job,{'doi':'10.1234/wrong'})
    for args in ({'url':'https://journal.example/wrong.pdf'},{'role':'wrong'},{'resource_id':wrong['id']}):
        with pytest.raises(CoverageError):value.check_download(job,candidate['id'],candidate['requirement_id'],**args)


def test_executor_redirect_chain_and_byte_change(ledger):
    value,job,root=ledger;article=seal(value,job)
    binding=bind(value,job,root,article['candidates'][0],final='https://cdn.example/file.pdf')
    assert binding['candidate_id']==article['candidates'][0]['id']
    Path(binding['path']).write_bytes(b'changed')
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='incomplete' and result['counts']['artifacts_verified']==0


def test_pmc_fallback_requires_real_higher_tier_attempts_and_final_version(ledger):
    value,job,_=ledger;article=seal(value,job);main=next(r for r in article['requirements'] if r['role']=='main_pdf')
    url='https://pmc.ncbi.nlm.nih.gov/articles/PMC123/pdf/file.pdf'
    evidence=snap(value,job,'https://pmc.ncbi.nlm.nih.gov/articles/PMC123/',url,authority='pmc',media_type='application/json')
    common={'url':url,'role':'main_pdf','requirement_id':main['id']}
    unknown=value.register_candidates(job,KEY,[common],source='pmc',snapshot_ids=[evidence])[0]
    with pytest.raises(CoverageError,match='published final'):value.check_download(job,unknown['id'],main['id'])
    final=value.register_candidates(job,KEY,[{**common,'version':'publishedVersion'}],source='pmc',snapshot_ids=[evidence])[0]
    for tier in ('journal','publisher'):
        with pytest.raises(CoverageError,match='Higher-authority'):value.check_download(job,final['id'],main['id'])
        value.record_attempt(job,KEY,tier,'access_required',source_url=ARTICLE,requirement_id=main['id'])
    assert value.check_download(job,final['id'],main['id'])['authority']=='pmc'


def test_unavailable_is_reported_but_never_complete(ledger):
    value,job,root=ledger;seal(value,job)
    value.record_attempt(job,KEY,'journal','access_required',source_url=ARTICLE)
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='incomplete' and result['counts']['assets_unavailable']==2
    assert all(g['kind']=='required_asset_unavailable' for g in result['gaps'])


def test_revision_does_not_reuse_prior_seals_or_candidates(ledger):
    value,job,root=ledger;seal(value,job)
    value.store.revise_job(job, value.store.get_job(job)['mission'])
    assert value.candidates(job)==[]
    assert audit_coverage(value.store,job,state_dir=root)['coverage_denominator'] is None


def test_legacy_job_never_promoted_and_journal_new_default(ledger):
    value,_,root=ledger
    mission=Mission(goal='Legacy',audit_contract='legacy_bounded',completeness='systematic').model_dump(mode='json')
    job=value.store.create_job(mission)
    assert audit_coverage(value.store,job['id'],state_dir=root)['status']=='legacy_bounded'
    assert Mission(goal='New',rune_id='journal.jacc').completeness=='systematic'
    assert Mission(goal='New').completeness=='bounded'
    assert Mission(goal='New').schema_version=='ore.mission/v2'


def test_artifact_without_binding_cannot_satisfy_a_requirement(ledger):
    value,job,root=ledger;seal(value,job)
    value.store.add_artifact(job,{'role':'main_pdf','status':'verified','integrity':'verified','sha256':'fake'})
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='incomplete' and result['counts']['artifacts_verified']==0


@pytest.mark.parametrize('journal,origin,row,title',[
    ('jacc','www.jacc.org','issue-item','issue-item__title'),
    ('circulation','www.ahajournals.org','issue-item','issue-item__title'),
    ('ehj','academic.oup.com','al-article-item','al-title'),
    ('jama-cardiology','jamanetwork.com','issue-article','article-title'),
    ('hir','e-hir.org','article-item','article-title'),
    ('plos-medicine','journals.plos.org','article','article-name'),
])
def test_bundled_publisher_profiles_close_fixture_lists(tmp_path,journal,origin,row,title):
    store=Store('sqlite:///:memory:');store.initialize()
    try:
        job=store.create_job(Mission(goal='Fixture templates',completeness='systematic').model_dump(mode='json'))['id']
        value=CoverageLedger(store,tmp_path)
        profile=value._profile(journal+'.issue.v1',('html_issue',))
        issue=f'https://{origin}/issue/fixture';article=f'https://{origin}/doi/10.1234/one'
        value.declare_collection(job,[issue])
        toc=f'<main data-total-articles="1"><div class="{row}"><div class="article-type">{profile["include_labels"][0]}</div><div class="{title}"><a href="{article}">Fixture</a></div></div></main>'
        value.seal_issue(job,issue,[snap(value,job,issue,toc)],journal+'.issue.v1')
        html=f'<meta name="citation_doi" content="10.1234/one"><meta name="citation_pdf_url" content="https://{origin}/main.pdf"><div class="article-type">{profile["include_labels"][0]}</div><div class="supplementary-material"><a href="/data.s001.csv">One</a><a href="/data.s002.xlsx">Two</a><a href="/data.s003.zip">Three</a></div>'
        result=value.seal_article(job,KEY,[snap(value,job,article,html)],journal+'.article.v1')
        assert len(result['requirements'])==4
        candidate=next(c for c in result['candidates'] if c['role']=='main_pdf')
        assert candidate['covered_authorities']==(['journal'] if journal=='jacc' else ['journal','publisher'])
        assert result['attachment_listing_complete'] is True
    finally:store.close()


def test_concurrent_identical_capture_is_one_immutable_snapshot(ledger):
    from concurrent.futures import ThreadPoolExecutor
    value,job,_=ledger
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids=list(pool.map(lambda _:snap(value,job,ISSUE,issue_html()),range(24)))
    assert len(set(ids))==1 and len(value._current(job,'snapshot'))==1
    assert value._get(job,'snapshot',ids[0])['sha256']==hashlib.sha256(issue_html().encode()).hexdigest()


def test_optional_scholarly_dependency_is_not_required_for_core_coverage(ledger,monkeypatch):
    import builtins
    value,job,root=ledger;original=builtins.__import__
    def blocked(name,*args,**kwargs):
        if name.startswith(('ore_scholarly','defusedxml')):raise ImportError('Fixture absent optional package')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',blocked)
    bare=CoverageLedger(value.store,root)
    assert bare.bundled=={} and bare._get(job,'collection','target')['issue_ids']==[ISSUE]


def test_relative_pdf_url_still_has_byte_evidence(ledger):
    value,job,_=ledger
    result=seal(value,job,article_html().replace(MAIN,'paper.pdf'))
    assert any(c['url']=='https://journal.example/doi/10.1234/paper.pdf' for c in result['candidates'])


def test_migrated_top_level_legacy_tag_preserves_bounded_contract(ledger):
    value,job,root=ledger
    value.store.update_job(job,audit_contract='legacy_bounded')
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='legacy_bounded' and result['applicable'] is False
    assert result['inventory_verified'] is False and result['coverage_denominator'] is None


def test_broad_publisher_research_label_does_not_include_review_title():
    from ore.coverage import _classify
    from ore_scholarly.coverage_profiles import bundled_profiles
    profiles={p['id']:p for p in bundled_profiles()}
    assert _classify('Research Article',profiles['plos-medicine.issue.v1'],'Effect estimates: a systematic review and meta-analysis')=='needs_review'
    assert _classify('Research',profiles['jama-cardiology.issue.v1'],'A study')=='needs_review'
    assert _classify('Original Investigation',profiles['jama-cardiology.issue.v1'],'A prospective cohort study')=='included'


def api_first_case(ledger, mode='api_open_access_first', *, seal_official=True):
    value,_,root=ledger
    mission=Mission(goal='Use approved API/OA transport first',completeness='systematic',
                    retrieval_policy={'mode':mode,'browser_fallback':False}).model_dump(mode='json')
    job=value.store.create_job(mission)['id'];value.declare_collection(job,[ISSUE])
    article=seal(value,job) if seal_official else None
    return value,job,root,article


def alternative_main(value,job,article,tier,version='publishedVersion'):
    main=next(r for r in article['requirements'] if r['role']=='main_pdf')
    origin='https://pmc.ncbi.nlm.nih.gov' if tier=='pmc' else 'https://publisher.example'
    url=origin+'/articles/one/final.pdf'
    evidence=snap(value,job,origin+'/resolver',url,authority=tier,media_type='application/json')
    candidate=value.register_candidates(job,KEY,[{'url':url,'doi':KEY[4:],'role':'main_pdf','requirement_id':main['id'],'version':version}],
                                        source=tier,snapshot_ids=[evidence])[0]
    return candidate


@pytest.mark.parametrize('tier',['publisher','pmc'])
def test_api_first_can_bind_evidenced_final_copy_without_higher_delivery_failures(ledger,tier):
    value,job,root,article=api_first_case(ledger)
    candidate=alternative_main(value,job,article,tier)
    assert value._current(job,'attempt')==[]
    assert value.check_download(job,candidate['id'],candidate['requirement_id'])['authority']==tier
    bind(value,job,root,candidate)
    incomplete=audit_coverage(value.store,job,state_dir=root)
    assert incomplete['status']=='incomplete' and incomplete['counts']['artifacts_verified']==1
    assert incomplete['counts']['supplements_expected']==1
    for supplement in article['candidates']:
        if supplement['role']=='supplement':bind(value,job,root,supplement)
    complete=audit_coverage(value.store,job,state_dir=root)
    assert complete['status']=='complete_within_scope' and complete['inventory_verified'] is True
    assert complete['coverage_denominator']==1 and complete['counts']['artifacts_verified']==2


def test_explicit_official_first_preserves_higher_delivery_attempt_gate(ledger):
    value,job,_,article=api_first_case(ledger,'official_first')
    candidate=alternative_main(value,job,article,'pmc')
    with pytest.raises(CoverageError,match='Higher-authority'):
        value.check_download(job,candidate['id'],candidate['requirement_id'])


@pytest.mark.parametrize('version',['unknown','acceptedVersion','submittedVersion'])
def test_api_first_still_rejects_unproven_or_nonfinal_main_versions(ledger,version):
    value,job,_,article=api_first_case(ledger)
    candidate=alternative_main(value,job,article,'pmc',version)
    with pytest.raises(CoverageError,match='published final'):
        value.check_download(job,candidate['id'],candidate['requirement_id'])


def test_api_first_does_not_create_official_inventory_from_resolver_metadata(ledger):
    value,job,root,_=api_first_case(ledger,seal_official=False)
    url='https://pmc.ncbi.nlm.nih.gov/articles/one/final.pdf'
    evidence=snap(value,job,'https://pmc.ncbi.nlm.nih.gov/resolver',url,authority='pmc',media_type='application/json')
    with pytest.raises(CoverageError,match='sealed article evidence is missing'):
        value.register_candidates(job,KEY,[{'url':url,'role':'main_pdf','version':'publishedVersion'}],source='pmc',snapshot_ids=[evidence])
    audit=audit_coverage(value.store,job,state_dir=root)
    assert audit['status']=='incomplete' and audit['coverage_denominator'] is None


def test_api_first_cannot_match_supplement_by_filename_or_change_bound_identity(ledger):
    value,job,_,article=api_first_case(ledger)
    url='https://pmc.ncbi.nlm.nih.gov/articles/one/data.csv'
    evidence=snap(value,job,'https://pmc.ncbi.nlm.nih.gov/resolver',url,authority='pmc',media_type='application/json')
    with pytest.raises(CoverageError,match='one explicit article artifact requirement'):
        value.register_candidates(job,KEY,[{'url':url,'role':'supplement','version':'unknown'}],source='pmc',snapshot_ids=[evidence])
    candidate=alternative_main(value,job,article,'pmc')
    wrong=value.store.upsert_resource(job,{'doi':'10.1234/different'})
    for arguments in ({'url':'https://pmc.ncbi.nlm.nih.gov/other.pdf'},{'role':'supplement'},{'resource_id':wrong['id']}):
        with pytest.raises(CoverageError):value.check_download(job,candidate['id'],candidate['requirement_id'],**arguments)


def all_types_case(ledger):
    value,_,root=ledger
    job=value.store.create_job(Mission(goal='Collect every article type',completeness='systematic',
                                      scope={'article_types':'all'}).model_dump(mode='json'))['id']
    value.declare_collection(job,[ISSUE])
    for kind in ('issue','article'):
        original=value._profile('fixture.'+kind,('html_'+kind,))
        profile={k:v for k,v in original.items() if k not in (
            'id','profile_digest','reviewer','state_version','job_id','generation','revision','created_at','updated_at')}
        value.register_profile({**profile,'id':'all.'+kind,'article_types':'all',
                                'ambiguous_title_patterns':['systematic review']},reviewer='all-types-fixture')
    return value,job,root


def test_all_article_scope_rejects_research_only_issue_profile(ledger):
    value,job,_=all_types_case(ledger)
    with pytest.raises(CoverageError,match="article_types='all'"):
        value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html(label='Editorial'))],'fixture.issue')
    assert value._current(job,'issue')==[]


def test_all_article_scope_rejects_research_only_article_profile(ledger):
    value,job,_=all_types_case(ledger)
    value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html())],'all.issue')
    with pytest.raises(CoverageError,match="article_types='all'"):
        value.seal_article(job,KEY,[snap(value,job,ARTICLE,article_html())],'fixture.article')
    assert value._current(job,'article')==[]


@pytest.mark.parametrize('label',['Original Article','Editorial','Review','CardioPulse','New Publisher Category',''])
def test_reviewed_all_type_profiles_keep_every_article_and_require_its_files(ledger,label):
    value,job,root=all_types_case(ledger)
    toc=issue_html(label=label).replace('>One<','>A systematic review<')
    issue=value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,toc)],'all.issue')
    assert issue['entries'][0]['classification']=='included'
    assert issue['entries'][0]['article_type_raw']==label
    article=value.seal_article(job,KEY,[snap(value,job,ARTICLE,article_html(label=label))],'all.article')
    assert article['classification']=='included'
    assert article['article_types_raw']==([label] if label else [])
    assert len(article['requirements'])==2
    incomplete=audit_coverage(value.store,job,state_dir=root)
    assert incomplete['status']=='incomplete' and incomplete['counts']['main_expected']==1
    assert incomplete['counts']['supplements_expected']==1 and incomplete['counts']['excluded']==0
    for candidate in article['candidates']:bind(value,job,root,candidate)
    complete=audit_coverage(value.store,job,state_dir=root)
    assert complete['status']=='complete_within_scope' and complete['inventory_verified'] is True
    assert complete['coverage_denominator']==1 and complete['counts']['resources_complete']==1
    assert complete['counts']['main_verified']==1 and complete['counts']['supplements_verified']==1


@pytest.mark.parametrize('body,match',[
    (article_html(label='Editorial').replace(f'<a class="main" href="{MAIN}">PDF</a>',''),'no declared main PDF'),
    (article_html(label='Editorial').replace(' data-complete',''),'not demonstrably complete'),
])
def test_all_type_profiles_preserve_pdf_and_supplement_evidence_requirements(ledger,body,match):
    value,job,_=all_types_case(ledger)
    value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html(label='Editorial'))],'all.issue')
    with pytest.raises(CoverageError,match=match):
        value.seal_article(job,KEY,[snap(value,job,ARTICLE,body)],'all.article')


def test_research_only_profile_preserves_editorial_exclusion(ledger):
    value,job,root=ledger
    issue=value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html(label='Editorial'))],'fixture.issue')
    assert issue['entries'][0]['classification']=='excluded'
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='complete_within_scope'
    assert result['counts']['excluded']==1 and result['counts']['main_expected']==0


def test_audit_rejects_legacy_all_article_job_sealed_with_research_only_issue(ledger,monkeypatch):
    value,job,root=all_types_case(ledger)
    # Reproduce the pre-fix ledger, which did not enforce mission article scope.
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(CoverageLedger,'_check_article_type_scope',lambda *args:None)
        value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html(label='Editorial'))],'fixture.issue')
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='incomplete' and result['inventory_verified'] is False
    assert result['coverage_denominator'] is None and result['counts']['issues_complete']==0
    assert result['gaps'][0]['kind']=='issue_manifest_unverified'
    assert "article_types='all'" in result['gaps'][0]['message']
    with pytest.raises(CoverageError,match="article_types='all'"):
        value.seal_article(job,KEY,[snap(value,job,ARTICLE,article_html(label='Editorial'))],'all.article')


def test_audit_rejects_legacy_research_article_profile_under_all_type_issue(ledger,monkeypatch):
    value,job,root=all_types_case(ledger)
    value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html())],'all.issue')
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(CoverageLedger,'_check_article_type_scope',lambda *args:None)
        article=value.seal_article(job,KEY,[snap(value,job,ARTICLE,article_html())],'fixture.article')
        for candidate in article['candidates']:bind(value,job,root,candidate)
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='incomplete' and result['inventory_verified'] is False
    assert result['counts']['resources_complete']==0 and result['counts']['artifacts_verified']==0
    assert result['gaps'][0]['kind']=='article_manifest_missing'
    assert "article_types='all'" in result['gaps'][0]['message']


def test_audit_rejects_legacy_exclusions_even_with_all_type_profile_marker(ledger,monkeypatch):
    import ore.coverage as coverage
    value,job,root=all_types_case(ledger)
    classify=coverage._classify
    # An older extractor can carry an unrecognized profile field while still
    # applying research-only exclusion labels. Its sealed counts are unsafe.
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(coverage,'_classify',lambda label,profile,title='':
                            classify(label,{k:v for k,v in profile.items() if k!='article_types'},title))
        value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html(label='Editorial'))],'all.issue')
    result=audit_coverage(value.store,job,state_dir=root)
    assert result['status']=='incomplete' and result['inventory_verified'] is False
    assert result['counts']['issues_complete']==0
    assert 'cannot exclude' in result['gaps'][0]['message']
