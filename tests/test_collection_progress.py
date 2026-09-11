"""Eligibility denominators, attachment evidence and measured dependency ETA."""
from datetime import datetime,timezone
from types import SimpleNamespace

from ore.progress import collection_progress_snapshot,workflow_progress_snapshot
from test_coverage import ledger,seal,bind,snap,article_html,issue_html,ISSUE,ARTICLE,KEY


def test_official_denominator_excludes_editorial_and_tracks_full_article(ledger):
    value,job,root=ledger
    extra='<div class="row"><span class="type">Editorial</span><a class="title" href="https://journal.example/doi/10.1234/editorial">Editorial</a></div>'
    value.seal_issue(job,ISSUE,[snap(value,job,ISSUE,issue_html(extra,count=2))],'fixture.issue')
    progress=collection_progress_snapshot(value.store,value.store.get_job(job),state_dir=root)
    assert progress['denominator']['status']=='sealed' and progress['denominator']['total']==1
    assert progress['resources']['discovered']==2 and progress['resources']['included']==1
    assert progress['resources']['complete']==0 and progress['supplements']['inventory_unknown']==1
    assert next(role for role in progress['roles'] if role['role']=='supplement')['expected'] is None
    article=value.seal_article(job,KEY,[snap(value,job,ARTICLE,article_html())],'fixture.article')
    for candidate in article['candidates']:bind(value,job,root,candidate)
    progress=collection_progress_snapshot(value.store,value.store.get_job(job),state_dir=root)
    assert progress['resources']=={'discovered':2,'included':1,'excluded':1,'classification_unresolved':0,'complete':1,'unresolved':0}
    assert progress['issues']=={'discovered':1,'complete':1,'total':1}
    assert all(role['expected']==role['verified']==1 for role in progress['roles'])
    assert progress['global_recall']=='unknown'
    artifact=value.store.artifacts(job)[0]
    from pathlib import Path
    Path(artifact['path']).write_bytes(b'changed')
    changed=collection_progress_snapshot(value.store,value.store.get_job(job),state_dir=root)
    assert changed['resources']['complete']==0 and changed['gaps']['count']>0


def test_confirmed_absence_is_zero_not_unknown(ledger):
    value,job,root=ledger;article=seal(value,job,article_html(supplements=False))
    for candidate in article['candidates']:bind(value,job,root,candidate)
    progress=collection_progress_snapshot(value.store,value.store.get_job(job),state_dir=root)
    supplement=next(role for role in progress['roles'] if role['role']=='supplement')
    assert supplement['expected']==supplement['verified']==supplement['unknown']==0
    assert progress['supplements']['none_confirmed']==1
    assert progress['resources']['complete']==1


def test_missing_authoritative_inventory_never_reports_denominator_zero(ledger):
    value,job,root=ledger
    progress=collection_progress_snapshot(value.store,value.store.get_job(job),state_dir=root)
    assert progress['denominator']['status']=='unknown' and progress['denominator']['total'] is None
    assert progress['issues']['total']==1 and progress['issues']['complete']==0


def test_generic_unknown_supplements_prevent_full_resource_completion():
    store=SimpleNamespace(resources=lambda _: [{'id':'article','classification':'included'}],
        artifacts=lambda _: [{'id':'file','resource_id':'article','role':'main_pdf','status':'verified'}])
    job={'id':'job','mission':{'artifact_roles':['main_pdf','supplement']}}
    progress=collection_progress_snapshot(store,job)
    assert progress['resources']['included']==1 and progress['resources']['complete']==0
    assert progress['supplements']['inventory_unknown']==1 and progress['supplements']['none_confirmed']==0
    assert progress['denominator']=={'status':'provisional','basis':'eligible registered resources; discovery is not a claim of complete source coverage','total':None}


def eta_fixture(parallel=True):
    nodes=[{'id':f'sample{i}','kind':'tool','status':'succeeded','task_id':f't{i}','spec':{'tool':'fetch'},'depends_on':[]} for i in range(5)]
    nodes += [{'id':ident,'kind':'tool','status':'pending','task_id':None,'spec':{'tool':'fetch'},'depends_on':deps} for ident,deps in [('a',[]),('b',[]),('c',['a','b'])]]
    def attempts(ident):
        i=int(ident[1:]);worker=f'w{i%2}' if parallel else 'w0'
        return [{'state':'succeeded','worker_id':worker,'started_at':'2026-01-01T00:00:00Z','finished_at':'2026-01-01T00:00:10Z'}]
    store=SimpleNamespace(attempts=attempts,artifacts=lambda _:[],resources=lambda _:[])
    job={'id':'job','mission':{'budget':{'max_agent_workers':8}}}
    return store,job,nodes


def test_eta_respects_dependency_join_and_observed_worker_parallelism():
    store,job,nodes=eta_fixture()
    progress=workflow_progress_snapshot(store,job,{'status':'running'},nodes,now=datetime(2026,1,1,tzinfo=timezone.utc))
    assert progress['eta']['remaining_active_seconds']=={'low':20,'high':20}
    assert progress['eta']['observed_parallelism']==2
    assert progress['eta']['critical_path_seconds']==20
    store,job,nodes=eta_fixture(parallel=False)
    assert workflow_progress_snapshot(store,job,{'status':'running'},nodes)['eta']['remaining_active_seconds']=={'low':30,'high':30}


def test_partial_lazy_foreach_expansion_and_user_wait_have_no_forecast():
    store,job,nodes=eta_fixture()
    nodes.append({'id':'discover','kind':'foreach','status':'waiting_children','spec':{},'iteration_items':[1,2,3],
                  'iteration_cursor':1,'children':['a'],'depends_on':[]})
    progress=workflow_progress_snapshot(store,job,{'status':'running'},nodes)
    assert progress['planned_total_known'] is False and progress['eta']['remaining_active_seconds'] is None
    assert workflow_progress_snapshot(store,job,{'status':'awaiting_user'},nodes)['eta']['remaining_active_seconds'] is None
