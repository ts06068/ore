"""Routing preferences do not grant access or assert corpus completeness."""
from unittest.mock import AsyncMock
import pytest
from pydantic import ValidationError
from ore.config import Settings
from ore.engine import Engine
from ore.models import Mission
from ore.routes import normalize_retrieval_policy, retrieval_plan, rank_candidates
from ore.tools import ToolRuntime


def test_pending_and_excluded_sources_are_skipped_without_becoming_ready():
    mission={'sources':['wos','pubmed','crossref'],'source_policy':{'exclude':{'search':['crossref']}}}
    profile={'source_readiness':{'wos':{'search':{'state':'approval_pending'}}}}
    plan=retrieval_plan(mission,profile)
    assert {r['source'] for r in plan['available'] if r['operation']=='search'}=={'pubmed'}
    assert {r['code'] for r in plan['skipped']} >= {'approval_pending','source_excluded','unconfigured'}
    assert next(r for r in plan['available'] if r['source']=='pubmed')['readiness']=='unknown'
    assert plan['authority_order']==['journal','publisher','pmc']
    assert not plan['inventory_verified_by_routing'] and plan['global_recall']=='unknown'


def test_api_first_ranks_equivalent_eligible_files_by_cost_without_granting_access():
    values=[{'url':'https://publisher.example/file.pdf','source':'publisher','version':'published_version'},
            {'url':'https://pmc.ncbi.nlm.nih.gov/file.pdf','source':'pmc','version':'published_version'},
            {'url':'https://pmc.ncbi.nlm.nih.gov/accepted.pdf','source':'pmc','version':'accepted_manuscript'}]
    mission={'scope':{'article_versions':['published_version']}}
    assert rank_candidates(values,mission)[0]['source']=='pmc'
    assert rank_candidates(values,{**mission,'retrieval_policy':{'mode':'official_first','browser_fallback':True}})[0]['source']=='publisher'
    mission['retrieval_policy']={'mode':'api_open_access_first','browser_fallback':False}
    rows=rank_candidates(values,mission)
    assert rows[0]['source']=='pmc' and not rows[-1]['routing']['eligible']
    mission['source_policy']={'exclude':{'download':['pmc']}}
    rows=rank_candidates(values,mission)
    assert rows[0]['source']=='publisher'
    assert not next(r for r in rows if r['url'].endswith('/file.pdf') and r['source']=='pmc')['routing']['eligible']


def test_policy_validates_and_is_snapshotted_on_creation(tmp_path):
    engine=Engine(Settings(state_dir=tmp_path,max_workers=0,auth_token='fixture'))
    try:
        engine.save_profile({'id':'preferred','retrieval_policy':{'mode':'api_open_access_first','browser_fallback':False}})
        job=engine.create({'goal':'Bounded API collection','access_profile':'preferred','sources':['pubmed']})
        assert job['mission']['retrieval_policy']=={'mode':'api_open_access_first','browser_fallback':False}
        assert '*' not in job['mission']['source_policy']['exclude'].get('browser',[])
        from ore.source_policy import authorize_operation
        assert authorize_operation(job['mission'],{},'jacc','browser')['code']=='browser_fallback_disabled'
        engine.save_profile({'id':'preferred','retrieval_policy':{'mode':'official_first','browser_fallback':True}})
        assert engine.store.get_job(job['id'])['mission']['retrieval_policy']['mode']=='api_open_access_first'
        assert normalize_retrieval_policy({'retrieval_policy':{'mode':'official_first'}},{'retrieval_policy':{'mode':'api_open_access_first'}})['mode']=='official_first'
        with pytest.raises(ValidationError):Mission(goal='Invalid',retrieval_policy={'mode':'make_up_mode'})
    finally:engine.store.close()


@pytest.mark.parametrize('url',[None,'https://www.jacc.org/toc/jacc/83/1','https://api.crossref.org/works'])
async def test_browser_disabled_before_any_browser_or_remote_allocation(tmp_path,url):
    engine=Engine(Settings(state_dir=tmp_path,max_workers=0,auth_token='fixture'))
    engine.browser.create=AsyncMock(side_effect=AssertionError('No browser may be created'))
    try:
        job=engine.create({'goal':'API only','sources':['crossref'],
                          'retrieval_policy':{'mode':'api_open_access_first','browser_fallback':False}})
        runtime=ToolRuntime(engine,job['id'])
        result=await runtime.execute('browser_open',{'url':url} if url else {})
        assert result['code']=='browser_fallback_disabled' and result['fallback_allowed']
        engine.browser.create.assert_not_awaited()
        state=await runtime.execute('state',{})
        assert state['retrieval_plan']['policy']['browser_fallback'] is False
        assert state['browsers']==[] and state['handoffs']==[]
    finally:engine.store.close()


async def test_profile_browser_denial_still_applies_to_older_mission(tmp_path):
    engine=Engine(Settings(state_dir=tmp_path,max_workers=0,auth_token='fixture'))
    engine.browser.create=AsyncMock(side_effect=AssertionError('No browser may be created'))
    try:
        engine.save_profile({'id':'preferred'})
        job=engine.create({'goal':'Older contract','access_profile':'preferred'})
        engine.save_profile({'id':'preferred','retrieval_policy':{'mode':'api_open_access_first','browser_fallback':False}})
        assert job['mission']['retrieval_policy']['browser_fallback'] is True
        result=await ToolRuntime(engine,job['id']).execute('browser_open',{})
        assert result['code']=='browser_fallback_disabled'
        plan=retrieval_plan(job['mission'],engine.profile(job['mission']))
        assert plan['policy']['browser_fallback'] is False
        engine.browser.create.assert_not_awaited()
    finally:engine.store.close()


def test_toggling_fallback_does_not_persist_a_derived_source_exclusion():
    from ore.source_policy import authorize_operation
    mission=Mission(goal='Switch routes',retrieval_policy={'mode':'api_open_access_first','browser_fallback':False}).model_dump(mode='json',exclude_none=True)
    assert authorize_operation(mission,{},'jacc','browser')['code']=='browser_fallback_disabled'
    mission['retrieval_policy']['browser_fallback']=True
    revised=Mission.model_validate(mission).model_dump(mode='json',exclude_none=True)
    assert authorize_operation(revised,{},'jacc','browser')['allowed']
    revised['source_policy']['exclude']['browser']=['jacc']
    assert authorize_operation(revised,{},'jacc','browser')['code']=='source_excluded'


def test_unclassified_sample_cannot_claim_supplement_discovery_complete(tmp_path):
    engine=Engine(Settings(state_dir=tmp_path,max_workers=0,auth_token='fixture'))
    try:
        job=engine.create({'goal':'One unclassified API sample','artifact_roles':['main_pdf','supplement']})
        engine.store.upsert_resource(job['id'],{'id':'unclassified-sample','title':'Candidate','classification':'needs_review','supplement_status':'present','expected_supplements':3})
        audit=engine.audit(job['id'])
        assert audit['status']=='incomplete' and audit['supplement_discovery_complete'] is False
    finally:engine.store.close()
