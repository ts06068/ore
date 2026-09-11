"""Persistent CAS and task fencing protect handoffs and sealed manifests."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from ore.store import Store, DocumentConflict, LeaseLost


def test_document_cas_survives_reopen_and_rejects_conflicting_writer(tmp_path):
    uri = f"sqlite:///{tmp_path / 'state.db'}"
    first, second = Store(uri), Store(uri)
    first.initialize()
    item = first.put_document('handoff', 'stable', {'status': 'open'}, expected_version=0)
    assert item['state_version'] == 1
    def change(store):
        try:
            return store.put_document('handoff', 'stable', {'status': 'claimed'}, expected_version=1)
        except DocumentConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, [first, second]))
    assert sum(result is not None for result in results) == 1
    first.close(); second.close()
    reopened = Store(uri)
    assert reopened.get_document('handoff', 'stable')['state_version'] == 2
    with pytest.raises(DocumentConflict):
        reopened.put_document('handoff', 'stable', {}, expected_version=0)
    reopened.close()


def test_document_task_lease_and_job_ownership_are_checked(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'state.db'}"); store.initialize()
    job = store.create_job({'goal': 'fixture'})
    other = store.create_job({'goal': 'other'})
    store.create_task(job['id'], 'retrieve', {}, 'one')
    task = store.claim_task('worker', job_id=job['id'])
    lease = {key: task[key] for key in ('worker_id', 'fence', 'revision')}
    lease['task_id'] = task['id']
    store.put_document('manifest', 'one', {'sealed': True}, job_id=job['id'], lease=lease)
    with pytest.raises(DocumentConflict):
        store.put_document('manifest', 'one', {}, job_id=other['id'])
    store.fail_task(task['id'], 'worker', task['fence'], {}, task['revision'], state='paused')
    with pytest.raises(LeaseLost):
        store.put_document('manifest', 'two', {}, job_id=job['id'], lease=lease)
    store.close()


def test_migration_keeps_legacy_result_and_artifact_history(tmp_path):
    from ore.migrate import upgrade
    uri = f"sqlite:///{tmp_path / 'state.db'}"
    store = Store(uri); store.initialize()
    job = store.create_job({'goal': 'legacy fixture'})
    store.update_job(job['id'], status='completed')
    original = store.get_job(job['id'])
    upgrade(uri)
    after = store.get_job(job['id'])
    assert after['status'] == 'completed'
    assert after['input_hash'] == original['input_hash']
    assert after['revision'] == original['revision']
    assert after['audit_contract'] == 'legacy_bounded'
    upgrade(uri)
    assert store.get_job(job['id'])['audit_contract'] == 'legacy_bounded'
    store.close()


@pytest.mark.asyncio
async def test_finite_collection_is_renewed_after_revision_and_refresh(tmp_path):
    from ore.engine import Engine
    from ore.config import Settings
    value=Engine(Settings(state_dir=tmp_path/'state',max_workers=0))
    issue='https://www.jacc.org/toc/jacc/83/1'
    try:
        job=value.create({'goal':'Collect a finite issue','issue_urls':[issue],'completeness':'systematic'})
        for operation in ('revise','refresh'):
            if operation=='revise':await value.revise(job['id'],{**value.store.get_job(job['id'])['mission'],'source_policy':{'exclude':{'search':['wos']}}})
            else:await value.refresh(job['id'])
            current=value.store.get_job(job['id'])
            rows=[r for r in value.store.list_documents('coverage.collection',job_id=job['id']) if r['revision']==current['revision']]
            assert len(rows)==1 and rows[0]['issue_ids']==[issue]
            assert rows[0]['generation']==current['generation']
            assert value.audit(job['id'])['status']!='complete_within_scope'
    finally:await value.stop()
