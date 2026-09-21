"""Source accounting stays atomic and independent of reread time."""
import json
import sqlite3
from unittest.mock import Mock
import pytest
from app.assistant.tests.non_agent_tests.test_belief_evidence_idempotent import store, _ev
from belief_engine.store.belief_store import BeliefUpsertRequest


@pytest.fixture(autouse=True)
def domain_config(monkeypatch):
    import belief_engine.config
    monkeypatch.setattr(belief_engine.config,'list_all_domain_ids',lambda:['routine'])


def row(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM user_beliefs WHERE id='b1'").fetchone())


def request(**kw):
    data = dict(domain='routine',belief_key='routine.x',statement='Only on weekday mornings',
                confidence='medium',scope='chronic',kind='episodic_context')
    data.update(kw)
    return BeliefUpsertRequest(**data)


def test_rereads_and_rewrites_do_not_reconfirm(store):
    s,E,db=store; s._chroma=Mock()
    for _ in range(14):
        s.upsert_belief(request(last_confirmed='2026-09-20'), [_ev(E,source_ref='chat:1')])
    s.upsert_belief(request(statement='More precise wording'), [])
    assert row(db)['observation_count']==1
    assert row(db)['last_confirmed']=='2026-09-01'
    s.add_evidence_to_existing('routine.x',[_ev(E,source_date='2026-08-01',source_ref='chat:2')])
    s.add_evidence_to_existing('routine.x',[_ev(E,source_date='2026-09-19',source_ref='chat:3',valence='contradict')])
    assert row(db)['observation_count']==3
    assert row(db)['first_observed']=='2026-08-01'
    assert row(db)['last_confirmed']=='2026-09-01'


def test_evidence_failure_rolls_back_claim_and_all_sources(store,monkeypatch):
    s,E,db=store; s._chroma=Mock()
    original=s._append_evidence
    calls=[]
    def fail(session,bid,ev,now):
        calls.append(ev)
        if len(calls)==2: raise RuntimeError('second insert failed')
        return original(session,bid,ev,now)
    monkeypatch.setattr(s,'_append_evidence',fail)
    with pytest.raises(RuntimeError):
        s.upsert_belief(request(),[_ev(E),_ev(E,source_date='2026-09-02')])
    assert row(db)['statement']=='x'
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM belief_evidence').fetchone()[0]==0


def test_index_failure_preserves_claim_with_evidence_and_retry_is_idempotent(store):
    s,E,db=store; s._chroma=Mock(); s._chroma.upsert.side_effect=RuntimeError('index unavailable')
    with pytest.raises(RuntimeError): s.upsert_belief(request(),[_ev(E)])
    assert row(db)['observation_count']==1
    s._chroma.upsert.side_effect=None
    s.upsert_belief(request(),[_ev(E)])
    assert row(db)['observation_count']==1


def test_missing_conditions_preserves_existing_and_locked_claim_is_unchanged(store):
    s,E,db=store; s._chroma=Mock()
    s.upsert_belief(request(conditions={'time':'morning'}),[_ev(E)])
    s.upsert_belief(request(),[])
    assert json.loads(row(db)['conditions'])=={'time':'morning'}
    with sqlite3.connect(db) as c: c.execute("UPDATE user_beliefs SET locked=1 WHERE id='b1'")
    s.upsert_belief(request(statement='Unconditional',conditions={}),[])
    assert row(db)['statement']=='Only on weekday mornings'


@pytest.mark.parametrize('source',['weekly_insights','decay_review','canonicalization'])
def test_interpretation_and_bookkeeping_cannot_reconfirm(store,source):
    s,E,db=store
    s._insert_evidence('b1',_ev(E,source_type=source),'2026-09-20')
    assert row(db)['observation_count']==0
    assert row(db)['last_confirmed'] is None


def test_snapshot_half_life_survives_kind_change(store):
    from datetime import datetime,timezone
    from belief_engine.decay.recompute import recompute_belief_snapshots
    s,E,db=store; s._chroma=Mock()
    s.upsert_belief(request(kind='episodic_context'),[_ev(E,weight=4.0)])
    s.upsert_belief(request(kind='durable_fact'),[])
    stats=recompute_belief_snapshots(now_utc=datetime(2026,9,15,tzinfo=timezone.utc))
    assert stats.errors==0
    assert row(db)['current_support_weight']==2.0


@pytest.mark.parametrize('kwargs',[{'weight':0.0},{'source_date':None},{'source_type':'weekly_insights'}])
def test_unusable_evidence_does_not_leave_stale_high_confidence(store,kwargs):
    from datetime import datetime,timezone
    from belief_engine.decay.recompute import recompute_belief_snapshots
    s,E,db=store
    s._insert_evidence('b1',_ev(E,**kwargs),'2026-09-20')
    with sqlite3.connect(db) as c: c.execute("UPDATE user_beliefs SET current_confidence_band='high',current_support_weight=10 WHERE id='b1'")
    stats=recompute_belief_snapshots(now_utc=datetime(2026,9,20,tzinfo=timezone.utc))
    assert stats.errors==0
    assert row(db)['current_confidence_band']=='unverified'
    assert row(db)['status']=='active'


def test_weekly_candidate_is_complete_stable_zero_weight_context(tmp_path,monkeypatch):
    from app.assistant.utils import path_utils
    from belief_engine.pipeline.steps.collect_evidence import _collect_weekly_insights
    monkeypatch.setattr(path_utils,'get_resources_dir',lambda:tmp_path)
    folder=tmp_path/'weekly_insights_pipeline_outputs'; folder.mkdir()
    candidate={'statement':'Full nuance '*100,'conditions':{'except':'Full condition '*100},'evidence':['full quote '*100]}
    data={'week_start':'2026-09-01','week_end':'2026-09-07','insights':{'belief_candidates':[candidate]}}
    (folder/'resource_weekly_insights_latest.json').write_text(json.dumps(data),encoding='utf-8')
    first=_collect_weekly_insights()[0]; second=_collect_weekly_insights()[0]
    assert json.loads(first.raw_text)['candidate']==candidate
    assert first.source_ref==second.source_ref
    assert first.weight==0


def test_new_unsupported_claim_has_no_fabricated_observation(store):
    s,E,db=store; s._chroma=Mock()
    created=s.upsert_belief(request(belief_key='routine.new',first_observed='2026-09-20',last_confirmed='2026-09-20'),[])
    assert created.observation_count==0
    assert created.first_observed is None
    assert created.last_confirmed is None


def test_durable_source_stays_durable_after_reclassification(store):
    from datetime import datetime,timezone
    from belief_engine.decay.recompute import recompute_belief_snapshots
    s,E,db=store; s._chroma=Mock()
    s.upsert_belief(request(kind='durable_fact'),[_ev(E,weight=3)])
    s.upsert_belief(request(kind='transient_state'),[])
    result=recompute_belief_snapshots(now_utc=datetime(2027,9,1,tzinfo=timezone.utc))
    assert result.errors==0
    assert row(db)['current_support_weight']==3


def test_concurrent_replay_attaches_once(store):
    from concurrent.futures import ThreadPoolExecutor
    s,E,db=store
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _:s._insert_evidence('b1',_ev(E),'2026-09-20'),range(6)))
    assert row(db)['observation_count']==1
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM belief_evidence').fetchone()[0]==1
