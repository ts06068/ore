from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio

import pytest

from ore import challenge_policy
from test_browser import browser_dependencies, site
from ore.challenge_policy import default_policy, normalize_policy
from ore.challenge_service import ChallengeCoordinator
from ore.store import Store, ControlConflict


@pytest.fixture
def clock(monkeypatch):
    value=[datetime.now(timezone.utc)]
    monkeypatch.setattr(challenge_policy, '_now', lambda:value[0])
    return value


@pytest.fixture
def setup(tmp_path):
    store=Store('sqlite:///'+str(tmp_path/'challenge.db'));store.initialize()
    policy=default_policy()
    job=store.create_job({'goal':'Elapsed challenge fixture','on_challenge':policy})
    yield store,job
    store.close()


def detected(store,job):
    return store.observe_challenge(job['id'],'https://fixture.test','public:operator',{'phase':'verification_visible','environment':'fixture'})


def test_model_thinking_counts_before_first_interaction(setup,clock):
    store,job=setup
    episode=detected(store,job)
    assert episode['attempts']==0 and episode['remaining_seconds']==120
    clock[0]+=timedelta(seconds=121)
    attempt=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
    assert attempt['allowed'] is False and attempt['attempts']==0
    assert attempt['stop_reason']=='elapsed_deadline'


def test_second_job_cannot_restart_shared_elapsed_episode(setup,clock):
    store,job=setup
    first=detected(store,job)
    clock[0]+=timedelta(seconds=60)
    other=store.create_job({'goal':'A second job','on_challenge':default_policy()})
    second=detected(store,other)
    assert second['started_at']==first['started_at'] and second['remaining_seconds']==60


def test_observed_progress_extends_only_to_approved_ceiling(setup,clock):
    store,job=setup;episode=detected(store,job)
    for count in range(1,7):
        clock[0]+=timedelta(seconds=5)
        reserved=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
        assert reserved['allowed']
        episode=store.finish_challenge(episode['id'],reserved['token'],evidence={
            'fingerprint':str(count),'phase':'verification_accepted','verification_accepted':True})
    assert episode['max_attempts']==6 and episode['max_elapsed_seconds']==300
    assert episode['state']=='awaiting_user' and episode['attempts']==6
    assert len([e for e in store.events(job['id']) if e['type']=='challenge.budget_adapted'])==3


def test_repeated_identical_failure_hands_off_early(setup,clock):
    store,job=setup;episode=detected(store,job)
    for _ in range(2):
        reservation=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
        episode=store.finish_challenge(episode['id'],reservation['token'],evidence={'fingerprint':'unchanged','phase':'verification_visible'})
    assert episode['attempts']==2 and episode['stop_reason']=='unchanged_challenge'
    assert episode['state']=='awaiting_user'


def test_extension_is_atomic_and_cannot_restart_expired_episode(setup,clock):
    store,job=setup;episode=detected(store,job)
    def extend(_):
        try:store.extend_challenge_budget(episode['id'],1,60,'fixture-operator');return True
        except ControlConflict:return False
    with ThreadPoolExecutor(max_workers=4) as workers:
        results=list(workers.map(extend,range(4)))
    assert sum(results)==3
    record=store.get_challenge(episode['id'])
    assert record['max_attempts']==6 and record['max_elapsed_seconds']==300
    clock[0]+=timedelta(seconds=301)
    with pytest.raises(ControlConflict,match='ended'):store.extend_challenge_budget(episode['id'],0,0,'fixture-operator')


def test_late_page_recovery_is_not_an_in_budget_success(setup,clock):
    store,job=setup;episode=detected(store,job)
    reservation=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
    clock[0]+=timedelta(seconds=121)
    done=store.finish_challenge(episode['id'],reservation['token'],resolved=True,evidence={'url':'https://fixture.test/article','substantive_content_observed':True})
    assert done['state']=='awaiting_user' and done['stop_reason']=='elapsed_deadline'
    assert not store.get_document('challenge.history',episode['id'])


def test_legacy_explicit_policy_keeps_its_active_clock(setup):
    assert normalize_policy({'max_attempts_per_episode':1,'max_active_seconds':3}).get('policy_version') is None
    store,job=setup
    row=store.reserve_challenge(job['id'],'https://legacy.test','public:operator',1,3)
    assert row.get('clock') is None
    assert store.finish_challenge(row['id'],row['token'])['state']=='awaiting_user'


@pytest.mark.parametrize('change',[{'hard_max_attempts':7},{'hard_max_elapsed_seconds':301},{'max_elapsed_seconds':float('nan')},{'adaptive':'yes'}])
def test_invalid_new_policy_is_rejected(change):
    with pytest.raises(ValueError):normalize_policy({**default_policy(),**change})


async def test_coordinator_hands_off_during_actor_thinking(setup,clock):
    store,job=setup;episode=detected(store,job)
    # Store deadline is already past real clock; no tool interaction triggers it.
    old=clock[0];clock[0]-=timedelta(seconds=200)
    other=store.create_job({'goal':'Expired fixture','on_challenge':default_policy()})
    expired=store.observe_challenge(other['id'],'https://expired.test','public:operator')
    clock[0]=old
    session=SimpleNamespace(closed=False,control='agent',challenge_id=expired['id'])
    async def takeover(sid):session.control='human'
    engine=SimpleNamespace(store=store,browser=SimpleNamespace(sessions={'sid':session},takeover=takeover),execution=SimpleNamespace(enabled=False))
    service=ChallengeCoordinator(engine)
    try:
        await service.tick();await asyncio.gather(*service.pending.values())
        assert session.control=='human'
        assert store.get_challenge(expired['id'])['state']=='awaiting_user'
    finally:await service.close()


@pytest.mark.browser
async def test_actual_browser_deadline_stops_while_actor_is_idle(tmp_path, browser_dependencies, site):
    from test_browser import make_manager
    manager,store,_,job,mission=make_manager(tmp_path,site)
    policy={**default_policy(),'max_elapsed_seconds':1.5,'hard_max_elapsed_seconds':2}
    mission['on_challenge']=policy
    # The persisted approved mission is the source for the elapsed clock.
    from sqlalchemy import update
    from ore.store import jobs
    with store._tx() as conn:conn.execute(update(jobs).where(jobs.c.id==job['id']).values(mission=mission))
    engine=SimpleNamespace(store=store,browser=manager,execution=SimpleNamespace(enabled=False))
    service=ChallengeCoordinator(engine)
    try:
        session=await manager.create(job['id'],mission,{'id':'public','allow_private_network':True})
        await manager.action(session.id,'navigate',{'url':site+'/challenge','screenshot':False})
        assert session.challenge_id
        assert store.get_challenge(session.challenge_id)['attempts']==0
        service.start()
        await asyncio.sleep(2)
        assert session.control=='human'
        assert store.get_challenge(session.challenge_id)['stop_reason']=='elapsed_deadline'
    finally:await service.close();await manager.close();store.close()


def test_expired_unacted_reservation_is_settled_and_human_can_resolve(setup,clock):
    from ore.store import LeaseLost
    store,job=setup;episode=detected(store,job)
    reserved=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
    clock[0]+=timedelta(seconds=121)
    expired=store.adapt_challenge(episode['id'])
    assert expired['token'] is None and expired['attempts']==1
    assert expired['active_seconds']==120 and expired['last_reservation']['status']=='expired'
    assert store.resolve_challenge(episode['id'],{'target_observed':True})['state']=='resolved'
    with pytest.raises(LeaseLost):store.finish_challenge(episode['id'],reserved['token'])


def test_same_policy_reobservation_does_not_reduce_adapted_allocation(setup,clock):
    store,job=setup;episode=detected(store,job)
    for i in range(3):
        reserved=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
        episode=store.finish_challenge(episode['id'],reserved['token'],evidence={'fingerprint':str(i),'phase':'accepted','verification_accepted':True})
    assert episode['max_attempts']==4 and episode['max_elapsed_seconds']==180
    again=detected(store,job)
    assert again['max_attempts']==4 and again['deadline_at']==episode['deadline_at']
    same=store.create_job({'goal':'Same approved policy','on_challenge':default_policy()})
    assert detected(store,same)['max_attempts']==4


def test_new_manual_participant_stops_elapsed_and_legacy_without_clock_conversion(setup,clock):
    store,job=setup;episode=detected(store,job)
    manual=store.create_job({'goal':'Manual only','on_challenge':{**default_policy(),'mode':'manual'}})
    result=detected(store,manual)
    assert result['state']=='awaiting_user' and result['stop_reason']=='manual_policy'
    assert not store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])['allowed']
    old=store.create_job({'goal':'Legacy','on_challenge':{'max_attempts_per_episode':3,'max_active_seconds':120}})
    reserved=store.reserve_challenge(old['id'],'https://legacy.test','public:operator')
    store.finish_challenge(reserved['id'],reserved['token'])
    joined=store.observe_challenge(manual['id'],'https://legacy.test','public:operator')
    assert joined.get('clock') is None
    assert not store.reserve_challenge(manual['id'],'https://legacy.test','public:operator')['allowed']


def test_legacy_join_preserves_active_constraint_without_elapsed_translation(setup,clock):
    store,job=setup;episode=detected(store,job)
    other=store.create_job({'goal':'Short active budget','on_challenge':{'max_attempts_per_episode':2,'max_active_seconds':3}})
    clock[0]+=timedelta(seconds=30)
    joined=detected(store,other)
    assert joined['clock']=='elapsed' and joined['remaining_seconds']==90
    assert joined['legacy_active_limit']==3
    reservation=store.reserve_challenge(other['id'],episode['origin'],episode['auth_context'])
    assert (datetime.fromisoformat(reservation['expires_at'])-clock[0]).total_seconds()==3
    clock[0]+=timedelta(seconds=4)
    done=store.adapt_challenge(episode['id'])
    assert done['token'] is None and done['active_seconds']==3 and done['state']=='awaiting_user'


def test_stricter_legacy_participant_cannot_take_third_attempt(setup,clock):
    store,job=setup;episode=detected(store,job)
    for i in range(2):
        item=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
        store.finish_challenge(item['id'],item['token'],evidence={'fingerprint':str(i)})
    other=store.create_job({'goal':'One attempt only','on_challenge':{'max_attempts_per_episode':1,'max_active_seconds':120}})
    result=store.reserve_challenge(other['id'],episode['origin'],episode['auth_context'])
    assert not result['allowed'] and result['attempts']==2 and result['max_attempts']==1


def test_episode_and_reservation_provenance_survive_other_job_observation(setup,clock):
    store,owner=setup;episode=detected(store,owner)
    reserver=store.create_job({'goal':'Reserve shared episode','on_challenge':default_policy()})
    observer=store.create_job({'goal':'Observe shared episode','on_challenge':default_policy()})
    reserved=store.reserve_challenge(reserver['id'],episode['origin'],episode['auth_context'])
    detected(store,observer)
    assert store.get_document('challenge',episode['id'])['job_id']==owner['id']
    finished=store.finish_challenge(episode['id'],reserved['token'],resolved=True,evidence={'target_observed':True})
    assert finished['episode_owner_job_id']==owner['id']
    assert finished['last_reservation']['job_id']==reserver['id']
    events=[e for e in store.events(reserver['id']) if e['type']=='challenge.finished']
    assert len(events)==1 and events[0]['payload']['episode_owner_job_id']==owner['id']
    assert not [e for e in store.events(observer['id']) if e['type']=='challenge.finished']


def test_changed_policy_restricts_same_job_once_but_identical_reobserve_does_not(setup,clock):
    from sqlalchemy import update
    from ore.store import jobs
    store,job=setup;episode=detected(store,job)
    store.extend_challenge_budget(episode['id'],1,60,'fixture-operator')
    stricter={**default_policy(),'max_attempts_per_episode':2,'max_elapsed_seconds':60}
    with store._tx() as conn:
        conn.execute(update(jobs).where(jobs.c.id==job['id']).values(mission={**job['mission'],'on_challenge':stricter}))
    restricted=detected(store,job)
    assert restricted['max_attempts']==2 and restricted['max_elapsed_seconds']==60
    extended=store.extend_challenge_budget(episode['id'],1,60,'fixture-operator')
    repeated=detected(store,job)
    assert repeated['max_attempts']==extended['max_attempts']==3
    assert repeated['deadline_at']==extended['deadline_at']


def test_reserve_cannot_reopen_resolved_episode_without_new_observation(setup,clock):
    store,job=setup;episode=detected(store,job)
    store.resolve_challenge(episode['id'],{'target_observed':True})
    clock[0]+=timedelta(seconds=121)
    denied=store.reserve_challenge(job['id'],episode['origin'],episode['auth_context'])
    assert not denied['allowed'] and denied['reason']=='observation_required'
    assert store.get_challenge(episode['id'])['state']=='resolved'
