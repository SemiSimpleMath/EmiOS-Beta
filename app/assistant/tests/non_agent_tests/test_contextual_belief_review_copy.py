from pathlib import Path
import json
import sqlite3
import pytest
from belief_engine.review.experiment import ReviewCopy, make_working_copy, investigate


@pytest.fixture
def store(tmp_path):
    base = tmp_path / 'base.db'
    with sqlite3.connect(base) as c:
        c.executescript('''
        CREATE TABLE user_beliefs(id TEXT PRIMARY KEY,belief_key TEXT,statement TEXT,
          domain TEXT,status TEXT,locked INTEGER,last_confirmed TEXT,confidence TEXT,
          conditions TEXT,updated_at TEXT,observation_count INTEGER);
        CREATE TABLE belief_evidence(id TEXT,belief_id TEXT,source_date TEXT,summary TEXT);
        CREATE TABLE unified_log_2026(id TEXT,timestamp TEXT,role TEXT,message TEXT,
          speaker_id TEXT,speaker_name TEXT,source TEXT,room_id TEXT);
        INSERT INTO user_beliefs VALUES('a','coffee.a','Enjoys coffee','food','active',0,'2026-01-01','high','weekday','old',1);
        INSERT INTO user_beliefs VALUES('b','sleep.b','Avoids caffeine late','health','active',0,'2026-01-01','high',NULL,'old',1);
        INSERT INTO belief_evidence VALUES('e','a','2026-01-01','An observation');
        INSERT INTO unified_log_2026 VALUES('m','2026-01-01 12:00:00','user','Only before noon',NULL,NULL,'chat','master_room');
        ''')
    work = tmp_path / 'work.db'
    make_working_copy(base, work)
    return ReviewCopy(base, work)


def context(store):
    return {'focal_belief_ids':['a'], 'inspected_beliefs':store.inspect(['a','b']), 'source_days':[], 'insights':[]}


def decision():
    return {'phase':'finish','search_queries':[],'inspect_belief_ids':[],'source_dates':[],
            'reasoning':'Evidence considered','revisions':[{
              'belief_id':'a','statement':'Enjoys coffee before noon','confidence':'medium',
              'status':'active','conditions':'Before noon','evidence_refs':['evidence:e'],
              'reasoning':'Source limits the claim','applicability':'Morning','uncertainty':'No other contexts known',
              'related_belief_ids':['b']}], 'preserved':[], 'unresolved':[], 'consumer_guidance':'Contextual guidance'}


def test_atomic_revision_retains_sources_and_observation_age(store):
    ctx=context(store); original=store.baseline.read_bytes()
    store.apply('one',decision(),ctx)
    row=store.inspect(['a'])[0]
    assert row['belief']['statement']=='Enjoys coffee before noon'
    assert row['belief']['last_confirmed']=='2026-01-01'
    assert row['belief']['observation_count']==1
    assert len(row['evidence'])==1
    assert store.baseline.read_bytes()==original
    with sqlite3.connect(store.working) as c:
        before,after=c.execute('SELECT before_json,after_json FROM contextual_review_revisions').fetchone()
        assert json.loads(before)['statement']=='Enjoys coffee'
        assert json.loads(after)['statement']=='Enjoys coffee before noon'


def test_replay_is_idempotent(store):
    ctx=context(store); result=decision()
    store.apply('one',result,ctx)
    assert store.apply('one',result,ctx)['replayed']
    result['consumer_guidance']='Changed'
    with pytest.raises(ValueError,match='Run ID reused'): store.apply('one',result,ctx)


@pytest.mark.parametrize('failure',['unknown_ref','locked','stale_context','unaccounted','duplicate'])
def test_rejects_invalid_revision_without_partial_write(store,failure):
    ctx=context(store); result=decision()
    if failure=='unknown_ref': result['revisions'][0]['evidence_refs']=['evidence:invented']
    if failure=='locked':
        with sqlite3.connect(store.working) as c: c.execute("UPDATE user_beliefs SET locked=1 WHERE id='a'")
        ctx=context(store)
    if failure=='stale_context':
        with sqlite3.connect(store.working) as c: c.execute("UPDATE user_beliefs SET statement='Changed context' WHERE id='b'")
    if failure=='unaccounted': result['revisions']=[]
    if failure=='duplicate': result['revisions']*=2
    with pytest.raises(ValueError): store.apply('bad',result,ctx)
    assert store.inspect(['a'])[0]['belief']['statement']=='Enjoys coffee'
    with sqlite3.connect(store.working) as c: assert c.execute('SELECT COUNT(*) FROM contextual_review_runs').fetchone()[0]==0


def test_transaction_rolls_back_first_write_if_later_target_locked(store):
    with sqlite3.connect(store.working) as c: c.execute("UPDATE user_beliefs SET locked=1 WHERE id='b'")
    ctx=context(store); ctx['focal_belief_ids']=['a','b']; result=decision()
    result['revisions'].append(dict(result['revisions'][0],belief_id='b'))
    with pytest.raises(ValueError,match='Owner-locked'): store.apply('bad',result,ctx)
    assert store.inspect(['a'])[0]['belief']['statement']=='Enjoys coffee'


def test_llm_can_expand_context_and_read_original_sources(store):
    calls=[]
    def invoke(name,payload):
        calls.append(name)
        if name.endswith('context_search'):
            assert len(payload['catalog'])==2
            return {'candidates':[{'belief_id':'b','reason':'Different topic connected to caffeine'}],'coverage_notes':'All current beliefs'}
        if not payload['source_days']:
            return dict(decision(),phase='investigate',revisions=[],source_dates=['2026-01-01'])
        assert payload['source_days'][0]['messages'][0]['message']=='Only before noon'
        result=decision()
        result['revisions'][0]['evidence_refs']=[payload['inspected_beliefs'][0]['evidence'][0]['ref']]
        return result
    result,ctx,trace=investigate(store,invoke,question='Coffee',focal_ids=['a'])
    assert len(trace)==3
    assert {p['belief']['id'] for p in ctx['inspected_beliefs']}=={'a','b'}
    store.apply('expanded',result,ctx)


def test_budget_exhaustion_never_writes(store):
    def invoke(name,payload):
        if name.endswith('context_search'): return {'candidates':[], 'coverage_notes':'No selection'}
        return dict(decision(),phase='investigate',revisions=[],source_dates=['2026-01-01'])
    with pytest.raises(RuntimeError,match='budget'): investigate(store,invoke,question='Coffee',focal_ids=['a'],max_rounds=1)
    assert store.inspect(['a'])[0]['belief']['statement']=='Enjoys coffee'


def test_refuses_unmarked_database(store):
    with pytest.raises((ValueError,sqlite3.OperationalError)): ReviewCopy(store.working,store.baseline)


def test_archived_predecessor_keeps_its_own_evidence_identity(store):
    with sqlite3.connect(store.working) as c:
        c.executescript('''
        CREATE TABLE user_beliefs_archive AS SELECT * FROM user_beliefs WHERE id='b';
        CREATE TABLE belief_evidence_archive AS SELECT * FROM belief_evidence WHERE 0;
        UPDATE user_beliefs_archive SET id='old';
        INSERT INTO belief_evidence_archive VALUES('old_e','old','2025-01-01','Original source');
        CREATE TABLE belief_merges(loser_id TEXT,survivor_id TEXT,reason TEXT);
        INSERT INTO belief_merges VALUES('old','a','Legacy merge');
        ''')
    packet=store.inspect(['a'])[0]
    assert packet['merged_predecessors'][0]['belief_id']=='old'
    old=store.inspect(['old'])[0]
    assert old['archived']
    assert old['evidence'][0]['ref']=='archived_evidence:old_e'
    assert packet['evidence'][0]['ref']=='evidence:e'


def test_changed_evidence_invalidates_review(store):
    ctx=context(store)
    with sqlite3.connect(store.working) as c:
        c.execute("UPDATE belief_evidence SET summary='Corrected source' WHERE id='e'")
    with pytest.raises(ValueError,match='evidence changed'): store.apply('bad',decision(),ctx)


def test_finished_with_pending_requests_is_not_applied(store):
    result=decision(); result['source_dates']=['2026-01-01']
    with pytest.raises(ValueError,match='incomplete'): store.apply('bad',result,context(store))


def test_source_handles_remain_stable_when_context_expands(store):
    from belief_engine.review.experiment import index_references
    ctx=context(store); index_references(ctx)
    handle=ctx['inspected_beliefs'][0]['evidence'][0]['ref']
    assert ctx['source_handles'][handle]=='evidence:e'
    ctx['inspected_beliefs']=store.inspect(['a','b'])
    ctx['source_days']=[store.source_day('2026-01-01')]
    index_references(ctx)
    assert ctx['inspected_beliefs'][0]['evidence'][0]['ref']==handle
    assert len(ctx['source_handles'])==2
    result=decision(); result['revisions'][0]['evidence_refs']=[handle]
    store.apply('short-handles',result,ctx)


def test_invalid_reference_is_returned_to_model_for_repair(store):
    from belief_engine.review.experiment import commit_review
    result=decision(); result['revisions'][0]['evidence_refs']=['wrong']
    seen=[]
    def invoke(name,payload):
        seen.append(payload['validation_error'])
        return decision()
    _,_,receipt=commit_review(store,invoke,run_id='repair',decision=result,context=context(store))
    assert seen and receipt['changed']==1
