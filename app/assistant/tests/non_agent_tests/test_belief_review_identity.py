import pytest
from belief_engine.matching import service as S


def packets():
    return [{'belief':{'id':bid},'evidence':[{'id':'e'+bid,'source_type':'daily_insights','raw_text':'Full nuance'}]} for bid in ('a','b')]


def test_citation_typo_retries_with_exact_source_labels(monkeypatch):
    calls=[]
    def call(factory,name,payload,scope,form):
        calls.append(payload)
        return {'relation':'different','evidence_ids':['typo'] if len(calls)==1 else ['E0','E1'],'equivalent_observations':[]}
    monkeypatch.setattr(S,'call_agent',call)
    a,b=packets();out=S.review(None,None,a,b)
    assert out['evidence_ids']==['ea','eb']
    assert calls[1]['a']['evidence'][0]['raw_text']=='Full nuance'
    assert a['evidence'][0]['id']=='ea'


def test_invalid_retry_cannot_authorize_mutation(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:{'relation':'same','evidence_ids':['unknown'],'equivalent_observations':[]})
    with pytest.raises(ValueError):S.review(None,None,*packets())
