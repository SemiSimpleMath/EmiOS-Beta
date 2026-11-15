"""Selection by each record's own belief_key. Since 2026-11-15 the evidence matcher's validate.py refuses a key not in
the catalog page and agent_runtime/correction.py corrects it once; select_records calls once. Invented data only."""
from types import SimpleNamespace
import pytest
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.utils.path_utils import get_app_root
from belief_engine.matching.selection import select_records
from app.assistant.agents.belief_engine.evidence_match.agent_form import AgentForm

RULES = AgentRegistry()._load_validator(
    get_app_root() / 'assistant' / 'agents' / 'belief_engine' / 'evidence_match' / 'validate.py')


def test_selects_complete_records_once_each_in_answer_order():
    records=[{'belief_key':'morning.only','statement':'text'*1000,'conditions':{'when':'morning'}},
             {'belief_key':'evening.only','statement':'different'}]
    calls=[]
    def invoke(rows):
        calls.append(rows)
        return {'belief_keys':['evening.only','morning.only','evening.only'], 'reasoning':'both'}
    selected,_=select_records(records,invoke,AgentForm)
    assert selected == [records[1],records[0]]
    assert selected[1] is records[0]
    assert len(calls)==1 and calls[0][0]['statement']==records[0]['statement']


def test_the_rules_refuse_a_key_not_in_the_catalog_and_keep_a_repeated_one():
    given={'evidence':[],'catalog':[{'belief_key':'a','statement':'s'},{'belief_key':'b','statement':'s'}]}
    assert RULES.validate({'belief_keys':['a','b','a'],'reasoning':'r'},given) == []
    bad=RULES.validate({'belief_keys':['a','S1','S1'],'reasoning':'r'},given)
    assert [(p.field,p.value,p.structural) for p in bad] == [('belief_keys','S1',True)]


def test_provider_errors_propagate_and_are_not_retried():
    calls=[]
    def broken(*args):
        calls.append(1)
        raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError):
        select_records([{'belief_key':'a'}],broken,AgentForm)
    assert len(calls)==1


def test_evidence_path_calls_once_and_caches_the_page(tmp_path, monkeypatch):
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
        return SimpleNamespace(data={'belief_keys':['actual.key'],'reasoning':'relevant'})
    factory=SimpleNamespace(create_agent=lambda _:SimpleNamespace(action_handler=run))
    evidence=[{'source_ref':'source1','raw_text':'complete original evidence'}]
    assert S.select_for_evidence(store,factory,None,evidence)==[{'id':'id1','belief_key':'actual.key','statement':'morning only'}]
    assert len(calls)==1 and calls[0]['evidence']==evidence and 'selection_retry' not in calls[0]
    assert [r['belief_key'] for r in calls[0]['catalog']]==['actual.key'] and 'id' not in calls[0]['catalog'][0]   # one id: no row id
    assert S.select_for_evidence(store,factory,None,evidence)==[{'id':'id1','belief_key':'actual.key','statement':'morning only'}]
    assert len(calls)==1

@pytest.fixture(autouse=True)
def isolated_selection_services(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    monkeypatch.setattr(ServiceLocator, '_services', {'agent_factory': SimpleNamespace(), 'global_blackboard': SimpleNamespace()})