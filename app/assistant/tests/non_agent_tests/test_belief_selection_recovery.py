from types import SimpleNamespace
import pytest
from belief_engine.matching.selection import select_records
from app.assistant.agents.dayflow_belief_selector.agent_form import AgentForm


def test_repairs_complete_selection_preserving_all_context():
    records=[{'belief_key':'morning.only','statement':'text'*1000,'conditions':{'when':'morning'}},
             {'belief_key':'evening.only','statement':'different'}]
    calls=[]
    def invoke(rows,retry):
        calls.append((rows,retry))
        return {'belief_ids':['morning.only'] if retry is None else ['B1','B0','B1'], 'reasoning':'both'}
    selected,_=select_records(records,invoke,AgentForm)
    assert selected == [records[1],records[0]]
    assert selected[1] is records[0]
    assert calls[0][0][0]['statement']==records[0]['statement']
    assert calls[1][1]['allowed_ids']==['B0','B1']
    assert calls[1][1]['previous_response']['belief_ids']==['morning.only']


def test_invalid_selection_never_partially_accepted():
    calls=[]
    def invoke(rows,retry):
        calls.append(retry)
        return {'belief_ids':['B0','B999'],'reasoning':'bad'}
    with pytest.raises(ValueError,match='after correction'):
        select_records([{'belief_key':'a'}],invoke,AgentForm)
    assert len(calls)==2


def test_malformed_output_repaired_but_provider_errors_not_retried():
    outputs=iter([None,{'belief_ids':[],'reasoning':'none relevant'}])
    assert select_records([{'belief_key':'a'}],lambda *_:next(outputs),AgentForm)==([], 'none relevant')
    calls=[]
    def broken(*args):
        calls.append(1)
        raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError):
        select_records([{'belief_key':'a'}],broken,AgentForm)
    assert len(calls)==1


def test_dayflow_retry_receives_weekly_context(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.pipelines.dayflow.steps.dayflow_routine_stage import _select_beliefs
    calls=[]
    def run(msg):
        calls.append(msg.agent_input)
        return SimpleNamespace(data={'belief_ids':['wrong'] if len(calls)==1 else ['B0'], 'reasoning':'relevant'})
    monkeypatch.setattr(DI,'agent_factory',SimpleNamespace(create_agent=lambda _:SimpleNamespace(action_handler=run)))
    records=[{'belief_key':'actual','statement':'morning only','conditions':{'when':'morning'}}]
    assert _select_beliefs(records,'day context',None,weekly_insights='whole weekly context')[0]==records
    assert calls[1]['weekly_insights']=='whole weekly context'
    assert calls[1]['selection_retry']['allowed_ids']==['B0']


def test_evidence_path_repairs_and_caches_only_valid_keys(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from dataclasses import make_dataclass
    from belief_engine.matching import service as S
    import belief_engine.db.paths as paths
    db=tmp_path/'test.db'
    @contextmanager
    def connection(*args,**kwargs):
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS belief_match_pages (receipt_key TEXT PRIMARY KEY, result_json TEXT)')
            yield conn
    monkeypatch.setattr(S,'connection',connection)
    monkeypatch.setattr(paths,'belief_db_path',lambda:db)
    monkeypatch.setattr(S,'packet',lambda *args:pytest.fail('Selection must not hydrate historical evidence'))
    Belief=make_dataclass('Belief',[('id',str),('belief_key',str),('statement',str)])
    store=SimpleNamespace(list_all=lambda **kwargs:[Belief('id1','actual.key','morning only')])
    calls=[]
    def run(msg):
        calls.append(msg.agent_input)
        return SimpleNamespace(data={'belief_ids':['invented'] if len(calls)==1 else ['B0'],'reasoning':'relevant'})
    factory=SimpleNamespace(create_agent=lambda _:SimpleNamespace(action_handler=run))
    evidence=[{'source_ref':'source1','raw_text':'complete original evidence'}]
    assert S.select_for_evidence(store,factory,None,evidence)==[{'id':'id1','belief_key':'actual.key','statement':'morning only'}]
    assert len(calls)==2 and calls[1]['evidence']==evidence
    assert S.select_for_evidence(store,factory,None,evidence)==[{'id':'id1','belief_key':'actual.key','statement':'morning only'}]
    assert len(calls)==2

@pytest.fixture(autouse=True)
def isolated_selection_services(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    monkeypatch.setattr(ServiceLocator, '_services', {'agent_factory': SimpleNamespace(), 'global_blackboard': SimpleNamespace()})