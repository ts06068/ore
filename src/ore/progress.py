"""Conservative ETA from persisted completed attempts, never synthetic progress."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
import statistics


def instant(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def progress_snapshot(store, job, handoffs=None, *, audit=None, now=None, state_dir=None):
    now = now or datetime.now(timezone.utc)
    if job.get('workflow_run_id'):
        run=store.get_document('workflow.run',job['workflow_run_id'])
        if run:
            return workflow_progress_snapshot(store,job,run,store.list_documents('workflow.node',job['id']),now=now,state_dir=state_dir)
    rows = [t for t in store.tasks(job['id']) if t['revision'] == job['revision'] and t['generation'] == job['generation']]
    counts = Counter(t['state'] for t in rows)
    artifacts = store.artifacts(job['id'])
    resources = store.resources(job['id'])
    pending = [t for t in rows if t['state'] not in ('succeeded', 'cancelled')]
    active_handoffs = [h for h in (handoffs or []) if h['status'] not in ('resolved', 'cancelled')
        and h.get('request_revision', h.get('revision', job['revision'])) == job['revision']
        and h.get('request_generation', h.get('generation', job['generation'])) == job['generation']]
    audit = audit or job.get('audit') or {}
    inventory_known = audit.get('inventory_verified') is True and audit.get('coverage_denominator') is not None
    samples = defaultdict(list)
    for task in rows:
        for attempt in store.attempts(task['id']):
            if attempt.get('state') != 'succeeded':
                continue
            start, end = instant(attempt.get('started_at')), instant(attempt.get('finished_at'))
            if start and end and end > start:
                samples[task['kind']].append((end - start).total_seconds())
    eta = {'state': 'estimating', 'remaining_active_seconds': None, 'finish_at': None,
           'sample_count': sum(map(len, samples.values())), 'basis': 'persisted successful attempts; active processing only',
           'reason': 'An official inventory and five completed samples per remaining stage are required.'}
    state = job.get('status', job.get('state'))
    stage = state or 'unknown'
    if state == 'completed':
        stage = 'complete'
        eta.update(state='complete', remaining_active_seconds={'low': 0, 'high': 0}, reason='The scoped completion audit passed.')
    elif state == 'finished_incomplete':
        eta.update(state='incomplete', reason='Execution ended with unresolved coverage gaps. Review the audit before another run.')
    elif state in ('needs_review', 'failed', 'cancelled'):
        eta.update(state=state, reason='The mission stopped without a completion forecast. Inspect the audit and recorded failures.')
    elif state in ('draft', 'paused', 'queued'):
        eta.update(state=state, reason='The mission is not actively running.')
    elif state == 'paused_budget' or any(t['state'] == 'paused_budget' for t in pending):
        stage = 'paused_budget'
        eta.update(state='paused_budget', reason='The execution budget is exhausted. A new budget and explicit resume are required before estimating completion.')
    elif active_handoffs or state in ('awaiting_user', 'awaiting_auth', 'waiting_external') or any(t['state'] in ('awaiting_user', 'awaiting_auth') for t in pending):
        external = state == 'waiting_external' or any(h.get('status') == 'waiting_external' for h in active_handoffs)
        stage = 'waiting_provider' if external else 'waiting_user'
        eta.update(state=stage, reason='User and provider waiting time cannot be predicted.')
    elif state == 'awaiting_source' or any(t['state'] == 'awaiting_source' for t in pending):
        stage = 'waiting_source'
        eta.update(state=stage, reason='A required source operation is unavailable. Its recovery time cannot be predicted.')
    elif state == 'blocked' or any(t['state'] in ('failed', 'blocked', 'paused') for t in pending):
        stage = 'blocked'
        eta.update(state='blocked', reason='Unresolved task failures or pauses prevent a completion forecast.')
    elif state != 'running':
        eta.update(state='unavailable', reason='The current mission state does not permit a completion forecast.')
    elif inventory_known and pending and all(len(samples[t['kind']]) >= 5 for t in pending):
        estimate=_measured_schedule(store,job,pending,samples,rows,lambda task:task['kind'],now=now)
        if estimate:eta.update(state='available',**estimate)
    if eta['state'] in ('estimating', 'available'):
        stage = next((t['kind'] for t in rows if t['state'] == 'running'), 'queued')
    return {'stage': stage,
            'tasks': dict(counts), 'task_total': len(rows), 'inventory_known': inventory_known,
            'resources_discovered': len(resources), 'artifacts_verified': sum(a.get('status') == 'verified' for a in artifacts),
            'artifacts_known': len(artifacts), 'needs_user': bool(active_handoffs), 'handoff_count': len(active_handoffs),
            'collection_progress':collection_progress_snapshot(store,job,audit=audit,state_dir=state_dir),
            'eta': eta, 'updated_at': now.isoformat()}


def workflow_progress_snapshot(store, job, run, nodes, *, now=None, state_dir=None):
    """Generic workflow progress; scope discovery and task duration stay distinct."""
    now = now or datetime.now(timezone.utc)
    active = [node for node in nodes if not node.get('superseded') and node.get('status') != 'superseded']
    counts = Counter(node['status'] for node in active)
    done = {'succeeded', 'skipped'}
    pending = [node for node in active if node['status'] not in done]
    unconfirmed = any(item.get('status') == 'unconfirmed' for item in run.get('interruptions', []))
    status = 'interrupting' if unconfirmed else run['status']
    # A running agent can still discover more nodes. Do not report a fixed
    # denominator or percentage based only on nodes currently materialized.
    known = not any(node['kind'] == 'agent' and node['status'] not in done or
                    node['kind'] == 'foreach' and ('iteration_items' not in node or node.get('iteration_cursor',0)<len(node['iteration_items'])) for node in active)
    def category(node):
        spec=node.get('spec', {})
        return spec.get('tool') or (node['kind'] + ':' + str(spec.get('purpose', '')))
    samples=defaultdict(list)
    for node in active:
        if not node.get('task_id'):continue
        for attempt in store.attempts(node['task_id']):
            if attempt.get('state') != 'succeeded':continue
            start, end=instant(attempt.get('started_at')), instant(attempt.get('finished_at'))
            if start and end and end > start:samples[category(node)].append((end-start).total_seconds())
    eta={'state':'estimating','remaining_active_seconds':None,'finish_at':None,
         'sample_count':sum(len(values) for values in samples.values()),
         'basis':'observed successful attempts for matching execution stages; active time only',
         'reason':'Awaiting finite workflow expansion and five successful duration samples per remaining stage.'}
    if status == 'completed':eta.update(state='complete',remaining_active_seconds={'low':0,'high':0},reason='Workflow completion checks passed.')
    elif status not in ('running','queued','resuming'):
        eta.update(state=status,reason='Execution is paused, awaiting resolution, or has unresolved checks.')
    elif any(node['status'] == 'retry_wait' and node.get('error', {}).get('code') == 'source_quota_wait' for node in active):
        waits = [node for node in active if node['status'] == 'retry_wait' and node.get('retry_at')]
        reset = min((instant(node['retry_at']) for node in waits), default=None)
        eta.update(state='waiting_source', next_retry_at=reset.isoformat() if reset else None,
                   reason='Waiting for a provider-reported quota reset; the existing execution budget still applies.')
    elif known and pending:
        estimate=_measured_schedule(store,job,pending,samples,active,category,now=now,workflow=True)
        if estimate:eta.update(state='available',**estimate)
    artifacts=store.artifacts(job['id'])
    return {'stage':status,'tasks':dict(counts),'task_total':len(active),'planned_total_known':known,
            'inventory_known':False,'artifacts_verified':sum(row.get('status')=='verified' for row in artifacts),
            'needs_user':status in ('awaiting_user','awaiting_auth','needs_reconciliation'),
            'collection_progress':collection_progress_snapshot(store,job,audit=run.get('domain_coverage'),state_dir=state_dir),
            'interrupt_confirmed':not unconfirmed,'eta':eta,'updated_at':now.isoformat()}


def collection_progress_snapshot(store, job, *, audit=None, state_dir=None):
    """Count eligible resources separately from all observed candidates/TOC rows."""
    mission=job.get('mission',{});strict=mission.get('completeness') in ('inventory','systematic') or mission.get('scope',{}).get('coverage_schema')=='ore.coverage/v1'
    if strict and state_dir is not None:
        from .coverage import audit_coverage
        audit=audit_coverage(store,job['id'],state_dir=state_dir)
    audit=audit or job.get('audit') or {}
    resources=store.resources(job['id']);artifacts=store.artifacts(job['id'])
    artifact_counts={'known':len(artifacts),'verified':sum(item.get('status')=='verified' for item in artifacts)}
    if strict:
        counts=audit.get('counts',{});gaps=audit.get('gaps',[])
        included=counts.get('toc_included',counts.get('included',0));complete=counts.get('resources_complete',0)
        issues_total=counts.get('issues_expected',0);issues_verified=counts.get('issues_verified',0)
        classification_unknown=counts.get('toc_unresolved',0)
        sealed=bool(issues_total) and issues_verified==issues_total and not classification_unknown and not any(
            item.get('kind') in ('collection_manifest_missing','issue_manifest_unverified','duplicate_issue_article') for item in gaps)
        unknown=counts.get('supplement_inventory_unknown',0)
        roles=[]
        for role,declared,verified in [('main_pdf',counts.get('main_expected',0),counts.get('main_verified',0)),
                                       ('supplement',counts.get('supplements_expected',0),counts.get('supplements_verified',0))]:
            expected=(included if role=='main_pdf' else declared) if sealed and (role=='main_pdf' or not unknown) else None
            roles.append({'role':role,'expected':expected,'declared':declared,'verified':verified,
                          'missing':max(0,(expected if expected is not None else declared)-verified),'unknown':0 if role=='main_pdf' and sealed else unknown})
        return {'kind':'scholarly','denominator':{'status':'sealed' if sealed else 'provisional' if issues_verified or resources else 'unknown',
                    'basis':'included classifications from verified official issue inventories; excludes noneligible TOC entries',
                    'total':included if sealed else None},
                'resources':{'discovered':max(len(resources),counts.get('resources_expected',0)),'included':included,
                    'excluded':counts.get('toc_excluded',counts.get('excluded',0)),'classification_unresolved':classification_unknown,
                    'complete':complete,'unresolved':max(0,included-complete)},
                'roles':roles,'artifacts':artifact_counts,
                'issues':{'discovered':issues_verified,'complete':counts.get('issues_complete',0),'total':issues_total or None},
                'supplements':{'none_confirmed':counts.get('supplements_none_confirmed',0),
                               'inventory_unknown':unknown,'present':counts.get('included_with_supplements',0)},
                'gaps':{'count':len(gaps),'by_kind':dict(Counter(item.get('kind','unknown') for item in gaps))},'global_recall':'unknown'}
    included=[item for item in resources if (item.get('classification') or item.get('eligibility')) in ('included','include','original_article')]
    excluded=[item for item in resources if (item.get('classification') or item.get('eligibility')) in ('excluded','exclude')]
    unknown=len(resources)-len(included)-len(excluded)
    roles=mission.get('artifact_roles',[]);rows=[];complete_ids={item['id'] for item in included};gap_counts=Counter()
    none_confirmed=supp_unknown=present=0
    for role in roles:
        expected=verified=declared=unknown_role=0
        for resource in included:
            matches=[artifact for artifact in artifacts if artifact.get('resource_id')==resource['id'] and artifact.get('role')==role
                     and artifact.get('status')=='verified' and artifact.get('integrity','verified')=='verified']
            versions=mission.get('scope',{}).get('article_versions',[])
            if role=='main_pdf' and versions:matches=[artifact for artifact in matches if artifact.get('version') in versions]
            verified+=len(matches)
            required=1
            if role=='supplement':
                absence=resource.get('supplement_status') in ('source_declares_none','verified_empty_manifest') and bool(resource.get('supplement_evidence'))
                if absence:none_confirmed+=1;required=0
                elif resource.get('supplement_status')=='present' and isinstance(resource.get('expected_supplements'),int):
                    present+=1;required=resource['expected_supplements']
                else:
                    supp_unknown+=1;unknown_role+=1;complete_ids.discard(resource['id']);gap_counts['supplement_inventory_unknown']+=1;continue
            declared+=required;expected+=required
            if len(matches)<required:
                complete_ids.discard(resource['id']);gap_counts['missing_verified_'+role]+=1
        rows.append({'role':role,'expected':expected if not unknown_role and not unknown else None,'declared':declared,
                     'verified':verified,'missing':max(0,declared-verified),'unknown':unknown_role})
    if unknown:gap_counts['classification_unresolved']=unknown
    inventory=job.get('inventory',{})
    sealed=bool(audit.get('inventory_verified')) and not unknown
    # A manifest count alone is declarative; it cannot seal a collection.
    total=len(included) if sealed else None
    return {'kind':'resources' if resources else 'artifacts','denominator':{'status':'sealed' if sealed else 'provisional' if resources else 'unknown',
                'basis':'eligible registered resources; discovery is not a claim of complete source coverage','total':total},
            'resources':{'discovered':len(resources),'included':len(included),'excluded':len(excluded),
                'classification_unresolved':unknown,'complete':len(complete_ids),'unresolved':len(included)-len(complete_ids)},
            'roles':rows,'artifacts':artifact_counts,'issues':{'discovered':0,'complete':0,'total':None},
            'supplements':{'none_confirmed':none_confirmed,'inventory_unknown':supp_unknown,'present':present},
            'gaps':{'count':sum(gap_counts.values()),'by_kind':dict(gap_counts)},'global_recall':'unknown'}


def _measured_schedule(store, job, pending, samples, all_rows, category, *, now, workflow=False):
    """List-schedule the remaining DAG at observed simultaneous worker capacity."""
    intervals=[];running={};workers=set()
    for row in all_rows:
        task_id=row.get('task_id') if workflow else row['id']
        if not task_id:continue
        for attempt in store.attempts(task_id):
            start=instant(attempt.get('started_at'));end=instant(attempt.get('finished_at'))
            worker=attempt.get('worker_id')
            if worker and start and end and end>start and attempt.get('state')=='succeeded':
                intervals.extend([(start,1,worker),(end,-1,worker)])
            if start and not end and row.get('status',row.get('state'))=='running':
                running[row['id']]=max(0,(now-start).total_seconds())
                if worker:workers.add(worker)
    active=Counter();peak=len(workers)
    for _,change,worker in sorted(intervals,key=lambda item:(item[0],item[1])):
        active[worker]+=change;peak=max(peak,sum(value>0 for value in active.values()))
    limit=job.get('mission',{}).get('budget',{}).get('max_agent_workers')
    slots=max(1,peak)
    if isinstance(limit,int) and limit>0:slots=min(slots,limit)
    ids={row['id'] for row in pending};durations={};dependencies={}
    for row in pending:
        waiting=workflow and row.get('status')=='waiting_children'
        values=sorted(samples[category(row)][-30:])
        if not waiting and len(values)<5:return None
        low=statistics.median(values) if values else 0
        high=values[min(len(values)-1,math.ceil(len(values)*.9)-1)] if values else 0
        elapsed=running.get(row['id'],0)
        if not waiting and elapsed>=high and elapsed>0:return None
        durations[row['id']]=(0,0) if waiting else (max(0,low-elapsed),max(0,high-elapsed))
        deps=list(row.get('depends_on',[])) if workflow else []
        if waiting:deps+=row.get('children',[])
        dependencies[row['id']]=set(deps)&ids
    def schedule(bound, capacity):
        finish={};available=[0.0]*capacity;remaining=set(ids)
        while remaining:
            ready=[ident for ident in remaining if dependencies[ident]<=finish.keys()]
            if not ready:return None
            ident=min(ready,key=lambda key:(max((finish[dep] for dep in dependencies[key]),default=0),0 if key in running else 1,key))
            ready_at=max((finish[dep] for dep in dependencies[ident]),default=0)
            duration=durations[ident][bound]
            if duration==0:finish[ident]=ready_at
            else:
                worker=min(range(capacity),key=available.__getitem__)
                finish[ident]=max(ready_at,available[worker])+duration;available[worker]=finish[ident]
            remaining.remove(ident)
        return max(finish.values(),default=0)
    low,high=schedule(0,slots),schedule(1,slots)
    if low is None:return None
    return {'remaining_active_seconds':{'low':math.ceil(low),'high':math.ceil(high)},
            'observed_parallelism':slots,'critical_path_seconds':math.ceil(schedule(0,max(1,len(ids)))),
            'basis':'persisted matching-stage durations, dependency graph, and observed simultaneous worker leases',
            'reason':'Median–90th percentile duration range scheduled through remaining dependencies at measured parallel capacity; external waiting excluded.'}
