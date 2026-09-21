import pytest
from belief_engine.matching import service as S


def packets():
    return [{'belief':{'id':bid},'evidence':[{'id':'e'+bid,'source_type':'daily_insights'}]} for bid in ('a','b')]


def test_rejected_proposal_preserves_both_claims(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:{'verdict':'preserve_separately','reason':'Additional recipient obligation','changed_meanings':['Scope changed'],'evidence_ids':['ea','eb']})
    proposal={'relation':'same','equivalent_observations':[['ea','eb']]}
    out=S.check_merge(None,None,*packets(),proposal)
    assert out['relation']=='unresolved' and out['equivalent_observations']==[]
    assert out['proposed_merge']==proposal


def test_approval_cannot_override_reported_lost_nuance(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:{'verdict':'approve','reason':'Equivalent except time','changed_meanings':['Time changed'],'evidence_ids':['ea','eb']})
    assert S.check_merge(None,None,*packets(),{'relation':'same'})['relation']=='unresolved'


def test_approval_requires_original_sources(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:{'verdict':'approve','reason':'Equivalent','changed_meanings':[],'evidence_ids':['ea']})
    result=S.check_merge(None,None,*packets(),{'relation':'same'})
    assert result['relation']=='unresolved' and result['merge_check_errors']


def test_distinct_claims_need_no_extra_call(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:pytest.fail('unnecessary call'))
    assert S.check_merge(None,None,*packets(),{'relation':'different'})['relation']=='different'


def test_partial_supersession_cannot_retire_independent_clause(monkeypatch):
    monkeypatch.setattr(S,'call_agent',lambda *args:{'verdict':'preserve_separately',
        'reason':'On-time remains valid','changed_meanings':['Would discard independent on-time'],
        'evidence_ids':['ea','eb']})
    proposal={'relation':'supersedes','current_id':'b'}
    result=S.check_merge(None,None,*packets(),proposal)
    assert result['relation']=='unresolved' and result['proposed_merge']==proposal
