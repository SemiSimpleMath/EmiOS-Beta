"""Model interpretation precedes deterministic evidence accounting."""
import json
import pytest
from belief_engine.pipeline.steps.collect_evidence import EvidenceItem
from belief_engine.pipeline.steps.update_beliefs import _assessed_evidence


def test_negative_statement_can_support_negative_claim_and_contradict_positive_claim():
    item=EvidenceItem('daily_insights','2026-09-01','chat:1','rejects','I dislike this reminder.',None,3)
    for valence in ('support','contradict'):
        result=_assessed_evidence([item],{'evidence_refs':[1],'evidence_relations':[{'evidence_ref':1,'valence':valence}]})
        assert result[0].valence==valence
        assert result[0].signal_type=='rejects'


@pytest.mark.parametrize('output',[
 {'evidence_refs':[1]},
 {'evidence_refs':[2],'evidence_relations':[{'evidence_ref':2,'valence':'support'}]},
 {'evidence_refs':[1],'evidence_relations':[{'evidence_ref':1,'valence':'support'},{'evidence_ref':1,'valence':'contradict'}]},
])
def test_missing_ambiguous_or_invented_assessments_are_refused(output):
    item=EvidenceItem('daily_insights','2026-09-01',None,'confirms','Source',None,3)
    with pytest.raises(ValueError): _assessed_evidence([item],output)


def test_rolling_ticket_context_retains_full_replies_without_inventing_intent(tmp_path,monkeypatch):
    from belief_engine.pipeline.steps import collect_evidence as C
    monkeypatch.setattr(C,'_day_context_root',lambda:tmp_path)
    monkeypatch.setattr(C,'_domain_ticket_types',lambda domain:['hydration'])
    dates=['2026-09-01','2026-09-02']
    monkeypatch.setattr(C,'_date_range',lambda n:iter(dates))
    entries=[]
    for day,state in zip(dates,['expired','accepted']):
        entry={'type':'ticket','suggestion_type':'hydration','state':state,'user_comment':'Full context '*300,'id':day}
        entries.append(entry)
        folder=tmp_path/'2026'/'09'/day;folder.mkdir(parents=True)
        (folder/'timeline_merged.json').write_text(json.dumps({'timeline':[entry]}),encoding='utf-8')
    items=C._collect_ticket_signals(None,14)
    assert len(items)==1 and items[0].source_type=='ticket_context'
    assert items[0].weight==0 and items[0].signal_type=='qualifies'
    assert [e['ticket'] for e in json.loads(items[0].raw_text)]==entries
    assert 'snooz' not in items[0].summary
    assert C._collect_ticket_signals(None,14)[0].source_ref==items[0].source_ref
    dates.pop()
    shorter=C._collect_ticket_signals(None,14)[0]
    assert shorter.source_ref!=items[0].source_ref and shorter.weight==0
