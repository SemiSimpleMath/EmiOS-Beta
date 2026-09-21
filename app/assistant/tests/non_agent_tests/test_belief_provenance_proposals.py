import pytest
from belief_engine.review import provenance as p

PAYLOAD = {'belief': {'statement': 'A scoped claim', 'conditions': {'when': 'morning'}},
           'sources': [{'source_id':'E0','record':{'id':'original','source_date':'2025-01-02','summary':'Full original text'}}]}

def test_complete_review_replays_without_model_and_invalidates_on_context_change(tmp_path,monkeypatch):
    calls=[]
    def call(*args):
        calls.append(args)
        return {'attributions':[{'source_id':'E0','relation':'qualify','reason':'Only part of the scoped claim is supported.'}]}
    monkeypatch.setattr(p,'call_agent',call)
    first=p.propose(None,None,'provenance_review',PAYLOAD,tmp_path)
    assert p.propose(None,None,'provenance_review',PAYLOAD,tmp_path)==first
    assert len(calls)==1
    changed={**PAYLOAD,'belief':{'statement':'Changed claim'}}
    p.propose(None,None,'provenance_review',changed,tmp_path)
    assert len(calls)==2
    import json
    saved=[json.loads(f.read_text()) for f in tmp_path.glob('*.json')]
    assert all(s['payload']['sources'][0]['record']==PAYLOAD['sources'][0]['record'] for s in saved)

@pytest.mark.parametrize('refs',[[],['E9'],['E0','E0']])
def test_missing_invented_or_duplicate_sources_fail_without_receipt(tmp_path,monkeypatch,refs):
    monkeypatch.setattr(p,'call_agent',lambda *a: {'attributions':[{'source_id':ref,'relation':'support','reason':'test'} for ref in refs]})
    with pytest.raises(ValueError,match='exactly once'):
        p.propose(None,None,'provenance_review',PAYLOAD,tmp_path)
    assert not list(tmp_path.glob('*.json'))

def test_discovery_cannot_invent_record(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'call_agent',lambda *a:{'candidates':[{'belief_id':'B0','candidate_id':'C9','reason':'test'}]})
    with pytest.raises(ValueError,match='unknown record'):
        p.propose(None,None,'provenance_discover',{'focal':[{'id':'B0'}],'catalog':[{'id':'C0'}]},tmp_path)
    assert not list(tmp_path.glob('*.json'))

def test_one_validated_repair_then_cached(tmp_path,monkeypatch):
    calls=[]
    def call(*args):
        calls.append(args)
        return {'attributions':[{'source_id':'unknown' if len(calls)==1 else 'E0','relation':'insufficient','reason':'Identity or context uncertain.'}]}
    monkeypatch.setattr(p,'call_agent',call)
    result=p.propose(None,None,'provenance_review',PAYLOAD,tmp_path)
    assert result['attributions'][0]['source_id']=='E0'
    assert len(calls)==2
    assert 'validation_error' in calls[1][2]['payload']
    p.propose(None,None,'provenance_review',PAYLOAD,tmp_path)
    assert len(calls)==2

def test_corrupt_context_receipt_is_rejected(tmp_path,monkeypatch):
    import json
    monkeypatch.setattr(p,'call_agent',lambda *a: {'attributions':[{'source_id':'E0','relation':'insufficient','reason':'Uncertain.'}]})
    p.propose(None,None,'provenance_review',PAYLOAD,tmp_path)
    file=next(tmp_path.glob('*.json')); receipt=json.loads(file.read_text())
    receipt['payload']['belief']['statement']='Another claim'
    file.write_text(json.dumps(receipt))
    with pytest.raises(ValueError,match='context mismatch'):
        p.propose(None,None,'provenance_review',PAYLOAD,tmp_path)

def test_exact_unique_supplied_key_is_an_identity_not_semantic_matching(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'call_agent',lambda *a: {'candidates':[{'belief_id':'current.key','candidate_id':'old.key','reason':'Potential source.'}]})
    result=p.propose(None,None,'provenance_discover',{'focal':[{'id':'B0','belief_key':'current.key'}],'catalog':[{'id':'C0','belief_key':'old.key'}]},tmp_path)
    assert result['candidates'][0]['belief_id']=='B0'
    assert result['candidates'][0]['candidate_id']=='C0'

def test_ambiguous_supplied_key_cannot_select_an_archive_version(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'call_agent',lambda *a: {'candidates':[{'belief_id':'B0','candidate_id':'old.key','reason':'Potential source.'}]})
    with pytest.raises(ValueError,match='unknown record'):
        p.propose(None,None,'provenance_discover',{'focal':[{'id':'B0'}],'catalog':[{'id':'C0','belief_key':'old.key'},{'id':'C1','belief_key':'old.key'}]},tmp_path)
