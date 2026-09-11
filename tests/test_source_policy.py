"""Operation allowlists and observed readiness, entirely offline."""
import pytest
from ore.source_policy import (SourceUnavailable, authorize_operation, normalize_source_policy,
    operation_for_url, require_operation, source_catalog, source_readiness)


def test_pending_search_does_not_disable_browser_or_fallback():
    mission={'sources':['wos','pubmed']}
    profile={'source_readiness':{'wos':{'search':{'state':'approval_pending'}}}}
    result=authorize_operation(mission,profile,'wos','search')
    assert result['code']=='approval_pending' and result['fallback_allowed']
    assert authorize_operation(mission,profile,'wos','browser')['allowed']
    assert authorize_operation(mission,profile,'pubmed','search')['allowed']


def test_explicit_empty_and_exclusion_win_over_wildcard():
    mission={'sources':['pubmed'], 'source_policy':{'allow':{'search':[],'browser':['*']},'exclude':{'browser':['wos']}}}
    assert not authorize_operation(mission,{},'pubmed','search')['allowed']
    assert authorize_operation(mission,{},'wos','browser')['code']=='source_excluded'
    assert not authorize_operation({'sources':[]},{},'pubmed','search')['allowed']


@pytest.mark.parametrize('url,expected',[
    ('https://api.clarivate.com/apis/wos-starter/v1/documents','search'),
    ('https://api.elsevier.com/content/search/scopus','search'),
    ('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi','search'),
    ('https://api.crossref.org/works','search'),
    ('https://api.unpaywall.org/v2/10.1234/one','resolve'),
    ('https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/','resolve'),
    ('https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/file.pdf','download'),
])
def test_api_cannot_be_relabelled_as_generic_fetch(url,expected):
    assert operation_for_url(url,'download')==expected
    if expected=='search':
        assert not authorize_operation({'sources':[]},{},'general_web','download',url=url)['allowed']


def test_profile_denials_combine_with_mission_and_required_is_explicit():
    mission={'sources':['pubmed','crossref'],'source_policy':{'required':{'search':['pubmed']}}}
    profile={'source_policy':{'allow':{'search':['pubmed']}},'sources':{'pubmed':{'enabled':False}}}
    policy=normalize_source_policy(mission,profile)
    assert policy['allow']['search']==['pubmed']
    result=authorize_operation(mission,profile,'pubmed','search')
    assert result['required'] and result['code']=='source_excluded'
    with pytest.raises(SourceUnavailable):require_operation(mission,profile,'pubmed','search')


def test_ready_requires_evidence_and_expiry_and_never_returns_credentials():
    profile={'source_readiness':{'pubmed':{'search':{'state':'ready','private':'do-not-return'}}}}
    assert source_readiness('pubmed','search',profile)['state']=='unknown'
    profile['source_readiness']['pubmed']['search'].update(evidence_ref='observation:fixture',observed_at='2026-09-10T00:00:00Z',expires_at='2020-01-01T00:00:00Z')
    result=source_readiness('pubmed','search',profile)
    assert result['state']=='unknown' and 'private' not in result
    profile['source_readiness']['pubmed']['search'].pop('expires_at')
    assert source_readiness('pubmed','search',profile)['state']=='ready'


def test_catalog_implementation_is_not_live_verification():
    catalog={r['id']:r for r in source_catalog({})}
    assert not catalog['pubmed']['live_verified']
    assert not catalog['google_scholar']['capabilities']['search']
    assert catalog['google_scholar']['readiness']['browser']['state']=='unknown'


@pytest.mark.parametrize('url,source',[
    ('https://api.clarivate.com/api/wos','wos_expanded'),
    ('https://api.dbpia.co.kr/v2/search/search.xml','dbpia'),
    ('https://apigateway.kisti.re.kr/openapicall.do','scienceon'),
    ('https://open.kci.go.kr/oai/request','kci'),
])
def test_actual_connector_endpoints_keep_source_and_operation(url,source):
    from ore.source_policy import source_for_url
    assert source_for_url(url)==source
    assert operation_for_url(url,'browser')=='search'
    assert not authorize_operation({'sources':[]},{},'general_web','browser',url=url)['allowed']


def test_required_pending_source_cannot_silently_disappear_from_final_audit():
    from ore.models import Mission
    from ore.source_policy import audit_required_operations
    from ore.store import Store
    store=Store('sqlite:///:memory:');store.initialize()
    try:
        job=store.create_job(Mission(goal='Required WoS use',sources=['wos'],source_policy={'required':{'search':['wos']}}).model_dump(mode='json'))
        store.record_observation(job['id'],{'kind':'source_unavailable','source':'wos','operation':'search','code':'approval_pending'})
        assert audit_required_operations(store,job['id'])[0]['kind']=='required_source_operation_unavailable'
        store.record_observation(job['id'],{'kind':'search','source':'wos','data':{'records':[]}})
        assert audit_required_operations(store,job['id'])==[]
        store.revise_job(job['id'],job['mission'])
        assert audit_required_operations(store,job['id'])[0]['kind']=='required_source_operation_unobserved'
    finally:store.close()
