"""Temporal evidence survives both production ask_kg payload builders."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.lib.core_tools.kg_search import knowledge_graph_search as K


def dt(day, month=9):
    return datetime(2026, month, day, tzinfo=timezone.utc)


@pytest.mark.parametrize('anchored', [False, True])
@pytest.mark.parametrize('timing', [
    {'start_date':dt(1), 'end_date':dt(5), 'valid_currently':False,
     'validity_reason':'User explicitly said the vacation ended.'},
    {'start_date':dt(1,10), 'end_date':dt(5,10)},
    {'end_date_confidence':'low', 'end_date_prose':'Perhaps early September',
     'validity_reason':'Estimated from silence, not confirmed by the user.'},
    {},
], ids=['expired','future','uncertain','undated'])
def test_answer_paths_preserve_temporal_evidence(monkeypatch, anchored, timing):
    person=K.Node(id='11111111-1111-4111-8111-111111111111',label='Morgan',node_type='Entity')
    state=K.Node(id='22222222-2222-4222-8222-222222222222',label='Vacation',node_type='State',
        description='Full qualification '*40+' FINAL EXCEPTION', attributes={'qualification':'Do not infer current availability'},
        first_observed=dt(2),last_observed=dt(3),created_at=dt(10),updated_at=dt(21),
        confidence=0.65,confidence_tier='provisional',observation_count=2,**timing)
    edge=K.Edge(id='33333333-3333-4333-8333-333333333333',source_id=person.id,target_id=state.id,
        relationship_type='has_state',sentence='Morgan is on vacation.',created_at=dt(10),updated_at=dt(21),
        confidence=0.7,confidence_tier='provisional')
    class Query:
        def __init__(self, rows): self.rows=rows
        def filter(self,*args): return self
        def all(self): return self.rows
        def count(self): return len(self.rows)
    class Session:
        def query(self, model): return Query([edge] if model is K.Edge else [person,state])
        def get(self,model,key): return state
    monkeypatch.setattr(K,'KnowledgeGraphUtils',lambda session:NS(create_embedding=lambda q:[1.,0.]))
    monkeypatch.setattr(K,'_expand_neighborhood_fast',lambda *a,**k:({state.id:0,person.id:1},[edge]))
    monkeypatch.setattr(K.Edge,'sentence_embedding',property(lambda self:[1.,0.]))
    monkeypatch.setattr('app.assistant.kg.chroma.chroma_embedding_manager.get_chroma_manager',
        lambda:NS(search_similar_edges=lambda *a,**k:[(edge.id,0.99)]))
    tool=object.__new__(K.KnowledgeGraphSearch)
    tool.session=Session()
    tool._run_rag_agent=lambda **kw:kw['evidence']
    def fail(error): raise AssertionError(error)
    tool.publish_error=fail
    evidence=tool.handle_ask_kg({'question':'Does this apply today?',**({'node':state.id} if anchored else {})},None)
    actual=next(n for n in evidence['nodes'] if n['node_id']==state.id)
    for name in ('start_date','end_date','first_observed','last_observed','created_at','updated_at'):
        expected=getattr(state,name)
        assert actual[name]==(expected.isoformat() if expected else None)
    for name in ('start_date_confidence','end_date_confidence','start_date_prose','end_date_prose',
                 'valid_currently','validity_reason','goal_status','confidence','confidence_tier','observation_count'):
        assert actual[name]==getattr(state,name)
    assert actual['description']==state.description
    assert actual['attributes']==state.attributes
    assert evidence['edges'][0]['updated_at']==dt(21).isoformat()
    assert evidence['edges'][0]['confidence']==0.7
    if anchored:
        assert evidence['base_node']==actual
