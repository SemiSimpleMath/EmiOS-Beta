"""Incremental matching contracts, against isolated SQLite and deterministic fake agents."""
import json
import sqlite3
from types import SimpleNamespace
import pytest
from belief_engine.db.schema import SCHEMA_SQL
from belief_engine.matching import service as S
from belief_engine.matching.context import packet, pages, observations, lineage
from belief_engine.matching.history import connection, pair_identity, apply_decision


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path/'beliefs.db')
    with connection(path, initialize=True) as c:
        c.executescript(SCHEMA_SQL)
        cols = {r[1] for r in c.execute('PRAGMA table_info(user_beliefs)')}
        for col, typ in [('locked','INTEGER DEFAULT 0'),('kind','TEXT')]:
            if col not in cols:
                c.execute(f'ALTER TABLE user_beliefs ADD COLUMN {col} {typ}')
        for bid in ('a','b','c'):
            c.execute('''INSERT INTO user_beliefs(id,belief_key,domain,statement,confidence,scope,status,
                conditions,observation_count,first_observed,last_confirmed,created_at,updated_at,kind)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (bid,bid,'routine',f'Complete claim {bid}','high','chronic','active',
                 '{"when":"morning"}',1,'2026-01-01','2026-01-01','2026-01-01','2026-01-01','stable_preference'))
            c.execute('''INSERT INTO belief_evidence(id,belief_id,source_type,source_date,signal_type,
                summary,raw_text,weight,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                ('e'+bid,bid,'daily_insights','2026-01-01','confirms','Source '+bid,'x'*5000+' END',3,'2026-01-01'))
    monkeypatch.setattr(S,'policy_version',lambda:'policy-1')
    return path


class Agents:
    def __init__(self, relation='different'):
        self.calls=[]
        self.relation=relation

    def create_agent(self, name):
        return SimpleNamespace(action_handler=lambda msg:self.respond(name,msg.agent_input))

    def respond(self,name,data):
        self.calls.append((name,data))
        if name.endswith('match_discover'):
            pairs=[{'focal_id':f['id'],'candidate_id':c['id'],'reason':'Potentially same'}
                   for f in data['focal'] for c in data['catalog'] if f['id']!=c['id']]
            return SimpleNamespace(data={'pairs':pairs,'reasoning':'Inspect sources'})
        if name.endswith('merge_check'):
            return SimpleNamespace(data={'verdict':'approve','reason':'Equivalent fixture claims',
                'changed_meanings':[], 'evidence_ids':[data['a']['evidence'][0]['id'],data['b']['evidence'][0]['id']]})
        a,b=data['a'],data['b']
        return SimpleNamespace(data={'relation':self.relation,'reason':'Full source judgment',
            'evidence_ids':[a['evidence'][0]['id'],b['evidence'][0]['id']],
            'survivor_id':a['belief']['id'],'current_id':b['belief']['id'],
            'canonical_statement':'Same fully qualified claim','conditions_json':json.dumps({'when':'morning'}),
            'scope':'chronic','kind':'stable_preference','equivalent_observations':[]})


def test_unchanged_night_has_no_model_calls(db):
    agents=Agents()
    first=S.run(db,agents,None)
    assert first['compared']==3
    count=len(agents.calls)
    second=S.run(db,agents,None)
    assert second['compared']==0 and len(agents.calls)==count


@pytest.mark.parametrize('sql',[
    "UPDATE user_beliefs SET conditions='{\"when\":\"evening\"}' WHERE id='a'",
    "UPDATE belief_evidence SET raw_text='New complete source' WHERE id='ea'",
])
def test_only_changed_belief_reopens_comparisons(db,sql):
    agents=Agents();S.run(db,agents,None)
    with connection(db) as c:c.execute(sql)
    result=S.run(db,agents,None)
    assert result['discovered']==1 and result['compared']==2


def test_counters_and_processing_dates_do_not_reopen(db):
    agents=Agents();S.run(db,agents,None)
    with connection(db) as c:
        c.execute("UPDATE user_beliefs SET observation_count=99,last_confirmed='today',updated_at='today'")
    count=len(agents.calls);S.run(db,agents,None)
    assert len(agents.calls)==count


def test_budget_resumes_without_repeating_pairs(db):
    agents=Agents()
    one=S.run(db,agents,None,comparison_limit=1)
    assert one['pending_comparisons']==2
    S.run(db,agents,None,comparison_limit=1)
    S.run(db,agents,None,comparison_limit=1)
    assert sum(name.endswith('match_review') for name,_ in agents.calls)==3
    assert S.run(db,agents,None)['compared']==0


def test_baseline_discovery_resumes(db):
    agents=Agents()
    assert S.run(db,agents,None,discovery_limit=1)['pending_discovery']==2
    assert S.run(db,agents,None,discovery_limit=1)['pending_discovery']==1
    assert S.run(db,agents,None,discovery_limit=1)['pending_discovery']==0
    assert sum(name.endswith('match_review') for name,_ in agents.calls)==3


def test_full_source_and_conditions_reach_both_agents(db):
    agents=Agents();S.run(db,agents,None)
    for name,data in agents.calls:
        if name.endswith('match_review'):
            assert data['a']['evidence'][0]['raw_text'].endswith(' END')
            assert len(data['a']['evidence'][0]['raw_text'])>5000
            assert 'morning' in data['a']['belief']['conditions']
        else:
            assert 'morning' in data['focal'][0]['conditions']
            assert data['focal'][0]['statement'].startswith('Complete claim')


def test_pages_never_cut_records():
    records=[{'statement':'x'*50000},{'statement':'y'*50000}]
    assert [r for p in pages(records,100) for r in p]==records


def test_invalid_model_result_stays_pending(db):
    class Bad(Agents):
        def respond(self,name,data):
            if name.endswith('match_review'):return SimpleNamespace(data={'relation':'garbage'})
            return super().respond(name,data)
    with pytest.raises(Exception):S.run(db,Bad(),None)
    with connection(db) as c:
        assert c.execute('SELECT COUNT(*) FROM belief_match_pairs WHERE applied=0').fetchone()[0]==3


def test_uncertainty_is_preserved_and_remembered(db):
    agents=Agents('unresolved');S.run(db,agents,None)
    assert S.run(db,agents,None)['compared']==0
    with connection(db) as c:assert c.execute("SELECT COUNT(*) FROM user_beliefs WHERE status='active'").fetchone()[0]==3


def test_merge_keeps_dates_conditions_lineage_without_new_evidence(db):
    agents=Agents('same');result=S.run(db,agents,None,comparison_limit=1)
    assert result['merges']==1
    with connection(db) as c:
        a=packet(c,'a')
        assert a['belief']['last_confirmed']=='2026-01-01'
        assert json.loads(a['belief']['conditions'])=={'when':'morning'}
        assert len(a['evidence'])==2
        assert c.execute('SELECT COUNT(*) FROM belief_evidence').fetchone()[0]==3
        assert c.execute('SELECT COUNT(*) FROM belief_match_merges').fetchone()[0]==1
        assert a['belief']['observation_count']==2


def test_archived_predecessor_evidence_is_read(db):
    S.run(db,Agents('same'),None,comparison_limit=1)
    with connection(db) as c:
        c.executescript('''CREATE TABLE user_beliefs_archive AS SELECT * FROM user_beliefs WHERE id='b';
            CREATE TABLE belief_evidence_archive AS SELECT * FROM belief_evidence WHERE belief_id='b';
            DELETE FROM belief_evidence WHERE belief_id='b';DELETE FROM user_beliefs WHERE id='b';''')
        assert {e['id'] for e in packet(c,'a')['evidence']}=={'ea','eb'}


def test_stale_write_rolls_back(db):
    with connection(db) as c:
        a,b=packet(c,'a'),packet(c,'b')
    decision=Agents('same').respond('match_review',{'a':a,'b':b}).data
    with connection(db) as c:c.execute("UPDATE belief_evidence SET raw_text='changed' WHERE id='ea'")
    with pytest.raises(ValueError,match='changed'):
        with connection(db) as c:apply_decision(c,'key',a,b,decision)
    with connection(db) as c:assert c.execute("SELECT status FROM user_beliefs WHERE id='b'").fetchone()[0]=='active'


def test_locked_merge_rejected(db):
    with connection(db) as c:c.execute("UPDATE user_beliefs SET locked=1 WHERE id='a'")
    with pytest.raises(ValueError,match='locked'):S.run(db,Agents('same'),None,comparison_limit=1)


def test_exact_observations_count_once_and_bookkeeping_never_counts(db):
    with connection(db) as c:
        c.execute("UPDATE belief_evidence SET source_ref='same-source', summary='identical', raw_text='same' WHERE belief_id IN ('a','b')")
    S.run(db,Agents('same'),None,comparison_limit=1)
    with connection(db) as c:
        assert len(observations(c,'a'))==1
        c.execute("UPDATE belief_evidence SET source_type='canonicalization',weight=100 WHERE belief_id IN ('a','b')")
        assert observations(c,'a')==[]


@pytest.mark.parametrize('relation,statuses',[('specialises',['active','active']),('contradicts',['contested','contested']),('supersedes',['deprecated','active'])])
def test_nonduplicate_relationships(db,relation,statuses):
    S.run(db,Agents(relation),None,comparison_limit=1)
    with connection(db) as c:
        assert [c.execute('SELECT status FROM user_beliefs WHERE id=?',(bid,)).fetchone()[0] for bid in ('a','b')]==statuses


def test_equivalent_source_observations_contribute_once(db):
    class SameSource(Agents):
        def respond(self,name,data):
            out=super().respond(name,data)
            if name.endswith('match_review'):
                out.data['equivalent_observations']=[['ea','eb']]
            return out
    S.run(db,SameSource('same'),None,comparison_limit=1)
    with connection(db) as c:
        assert len(observations(c,'a'))==1
        assert len(packet(c,'a')['evidence'])==2
        assert packet(c,'a')['belief']['observation_count']==1


def test_late_merge_validation_failure_rolls_back_every_write(db):
    class InvalidGroup(Agents):
        def respond(self,name,data):
            out=super().respond(name,data)
            if name.endswith('match_review'):out.data['equivalent_observations']=[['ea','invented']]
            return out
    with pytest.raises(ValueError):S.run(db,InvalidGroup('same'),None,comparison_limit=1)
    with connection(db) as c:
        assert packet(c,'a')['belief']['statement']=='Complete claim a'
        assert packet(c,'b')['belief']['status']=='active'
        assert c.execute('SELECT COUNT(*) FROM belief_merges').fetchone()[0]==0


def test_policy_change_reopens_review(db,monkeypatch):
    agents=Agents();S.run(db,agents,None)
    monkeypatch.setattr(S,'policy_version',lambda:'policy-2')
    assert S.run(db,agents,None)['compared']==3


def test_evidence_selection_is_cached_and_returns_complete_current_claims(db,monkeypatch):
    from dataclasses import fields
    from belief_engine.store.belief_store import BeliefRecord
    import belief_engine.db.paths as paths
    monkeypatch.setattr(paths,'belief_db_path',lambda:db)
    with connection(db) as c:
        names={f.name for f in fields(BeliefRecord)}
        records=[BeliefRecord(**{k:v for k,v in dict(row).items() if k in names})
                 for row in c.execute('SELECT * FROM user_beliefs')]
    calls=[]
    class Factory:
        def create_agent(self,name):
            def call(msg):
                calls.append(msg.agent_input)
                return SimpleNamespace(data={'belief_ids':[next(r['selection_id'] for r in msg.agent_input['catalog'] if r['belief_key']=='a')],'reasoning':'Relevant source'})
            return SimpleNamespace(action_handler=call)
    store=SimpleNamespace(list_all=lambda **_:records)
    evidence=[{'raw_text':'z'*6000+' END','source_date':'2026-01-01'}]
    first=S.select_for_evidence(store,Factory(),None,evidence)
    S.select_for_evidence(store,Factory(),None,evidence)
    assert len(calls)==1 and first[0]['statement']=='Complete claim a'
    assert first[0]['conditions']=='{"when":"morning"}'
    assert 'evidence' not in first[0]
    assert calls[0]['evidence']==evidence


def test_index_failure_leaves_durable_retry(db,monkeypatch):
    import belief_engine.chroma.belief_chroma as chroma
    S.run(db,Agents('same'),None,comparison_limit=1)
    def fail(**kwargs):raise RuntimeError('index offline')
    monkeypatch.setattr(chroma,'get_belief_chroma',lambda:SimpleNamespace(upsert=fail))
    with pytest.raises(RuntimeError,match='offline'):S.sync_indexes(db)
    with connection(db) as c:assert c.execute('SELECT index_synced FROM belief_match_merges').fetchone()[0]==0
    monkeypatch.setattr(chroma,'get_belief_chroma',lambda:SimpleNamespace(upsert=lambda **_:None,delete_many=lambda _:None))
    S.sync_indexes(db)
    with connection(db) as c:assert c.execute('SELECT index_synced FROM belief_match_merges').fetchone()[0]==1


def test_decay_uses_original_sources_and_ignores_legacy_merge_weight(db,monkeypatch):
    from datetime import datetime,timezone
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from belief_engine.decay import recompute
    import belief_engine.db.paths as paths
    S.run(db,Agents('same'),None,comparison_limit=1)
    with connection(db) as c:
        c.execute("INSERT INTO belief_evidence(id,belief_id,source_type,source_date,signal_type,summary,weight,created_at) "
                  "VALUES ('bookkeeping','a','canonicalization','2026-01-01','confirms','not support',100,'2026-01-01')")
    engine=create_engine('sqlite:///'+db)
    monkeypatch.setattr(recompute,'get_session',sessionmaker(bind=engine))
    monkeypatch.setattr(paths,'belief_db_path',lambda:db)
    stats=recompute.recompute_belief_snapshots(now_utc=datetime(2026,1,1,tzinfo=timezone.utc))
    assert stats.errors==0
    with connection(db) as c:
        assert c.execute("SELECT current_support_weight FROM user_beliefs WHERE id='a'").fetchone()[0]==6.0
    engine.dispose()


def test_standard_evidence_reader_follows_merge_lineage(db,monkeypatch):
    from belief_engine.store.belief_store import BeliefStore
    import belief_engine.db.paths as paths
    S.run(db,Agents('same'),None,comparison_limit=1)
    monkeypatch.setattr(paths,'belief_db_path',lambda:db)
    store=object.__new__(BeliefStore)
    assert {e.id for e in store.get_evidence('a')}=={'ea','eb'}


def test_oversized_pair_does_not_stop_other_pairs_and_is_not_repeated(db, monkeypatch):
    from belief_engine.matching.investigation import ReviewBudgetExceeded
    original = S.review
    attempts = []
    def review(factory, scope, a, b, **kwargs):
        attempts.append((a['belief']['id'], b['belief']['id']))
        if set(attempts[-1]) == {'a','b'}:
            raise ReviewBudgetExceeded('Complete findings exceed budget')
        return original(factory, scope, a, b, **kwargs)
    monkeypatch.setattr(S, 'review', review)
    agents = Agents()
    first = S.run(db, agents, None)
    assert first['compared'] == 2
    assert first['blocked_review_budget'] == 1
    assert first['pending_comparisons'] == 1 and first['status'] == 'pending'
    count = len(attempts)
    second = S.run(db, agents, None)
    assert len(attempts) == count and second['blocked_review_budget'] == 1
    with connection(db) as c:
        assert c.execute("SELECT COUNT(*) FROM user_beliefs WHERE status='active'").fetchone()[0] == 3
        c.execute("UPDATE belief_evidence SET raw_text='Changed evidence' WHERE id='ea'")
    S.run(db, agents, None)
    assert len(attempts) > count
