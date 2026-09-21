import pytest
from belief_engine.matching import service as S


def test_self_pairs_are_ignored_and_focal_pairs_are_valid(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:{'pairs':[
        {'focal_id':'a','candidate_id':'a','reason':'self'},
        {'focal_id':'a','candidate_id':'b','reason':'two focal'},
        {'focal_id':'b','candidate_id':'a','reason':'repeat'}]})
    result=S.discover(None,None,[{'id':'a'},{'id':'b'}],[{'id':'c'}])
    assert len(result['pairs'])==1 and result['pairs'][0]['candidate_id']=='b'


def test_unknown_uuid_retries_once_with_local_labels(monkeypatch):
    calls=[]
    def call(factory,name,payload,scope,form):
        calls.append(payload)
        return {'pairs':[{'focal_id':'typo' if len(calls)==1 else 'B0','candidate_id':'B1','reason':'possible'}]}
    monkeypatch.setattr(S,'call_agent',call)
    result=S.discover(None,None,[{'id':'actual-a','statement':'all nuance'}],[{'id':'actual-b'}])
    assert result['pairs'][0]['focal_id']=='actual-a'
    assert calls[1]['focal'][0]['statement']=='all nuance'


def test_repeated_invalid_ids_leave_discovery_pending(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:{'pairs':[{'focal_id':'invented','candidate_id':'invented','reason':'bad'}]})
    with pytest.raises(ValueError):S.discover(None,None,[{'id':'a'}],[{'id':'b'}])
