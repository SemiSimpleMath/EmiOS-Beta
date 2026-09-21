import json
import pytest
from belief_engine.matching import investigation as I
from belief_engine.matching.context import encode


def packets():
    a={'belief':{'id':'a','statement':'Morning only; never evenings','conditions':{'when':'morning'}},'version':'a1',
       'lineage':[], 'evidence':[{'id':'ea','source_type':'daily_insights','raw_text':'Full source '*3000+' never evenings END'}]}
    b={'belief':{'id':'b','statement':'Morning only'},'version':'b1','lineage':[],
       'evidence':[{'id':'eb','source_type':'daily_insights','raw_text':'Original confirmation'}]}
    return a,b


def test_fragments_reconstruct_every_source_without_clipping():
    a,b=packets();parts=I.fragments_for(a,b)
    for side,p in [('a',a),('b',b)]:
        for e in p['evidence']:
            actual=''.join(f['text'] for f in parts if f['side']==side and f['record_id']==e['id'])
            assert actual==encode(e)


def test_all_pages_are_read_and_receipts_resume(tmp_path,monkeypatch):
    monkeypatch.setattr(I,'DIRECT_LIMIT',10000)
    calls=[]
    def call(factory,name,payload,scope,form):
        calls.append((name,payload))
        if name.endswith('evidence_page'):
            return {'findings':[{'fragment_id':f['fragment_id'],'findings':'Preserve morning-only exception; full source was read.'} for f in payload['fragments']]}
        return {'relation':'unresolved'}
    a,b=packets();db=tmp_path/'copy.db'
    I.review_pair(db,None,None,a,b,'policy',call,None)
    assert any(name.endswith('evidence_page') for name,_ in calls)
    final=calls[-1][1]
    seen={f['fragment_id'] for page in final['page_reviews'] for f in page['fragments']}
    assert seen=={f['fragment_id'] for f in I.fragments_for(a,b)}
    assert final['a']['belief']==a['belief']
    count=sum(name.endswith('evidence_page') for name,_ in calls)
    I.review_pair(db,None,None,a,b,'policy',call,None)
    assert sum(name.endswith('evidence_page') for name,_ in calls)==count


def test_missing_fragment_review_refuses_final_decision(tmp_path,monkeypatch):
    monkeypatch.setattr(I,'DIRECT_LIMIT',10000)
    def call(*args):return {'findings':[]}
    with pytest.raises(ValueError,match='omitted'):
        I.review_pair(tmp_path/'copy.db',None,None,*packets(),'policy',call,None)


def test_oversized_findings_remain_pending_without_truncation(tmp_path,monkeypatch):
    monkeypatch.setattr(I,'DIRECT_LIMIT',10000)
    def call(factory,name,payload,scope,form):
        assert name.endswith('evidence_page')
        return {'findings':[{'fragment_id':f['fragment_id'],'findings':'Important nuance '*2000} for f in payload['fragments']]}
    with pytest.raises(ValueError,match='without truncation'):
        I.review_pair(tmp_path/'copy.db',None,None,*packets(),'policy',call,None)


def test_merge_guard_reuses_complete_source_pages(tmp_path,monkeypatch):
    monkeypatch.setattr(I,'DIRECT_LIMIT',10000)
    calls=[]
    def call(factory,name,payload,scope,form):
        calls.append((name,payload))
        if name.endswith('evidence_page'):
            return {'findings':[{'fragment_id':f['fragment_id'],'findings':'Complete qualifier retained.'} for f in payload['fragments']]}
        return {'relation':'unresolved'}
    a,b=packets();db=tmp_path/'copy.db'
    I.review_pair(db,None,None,a,b,'policy',call,None)
    before=sum(name.endswith('evidence_page') for name,_ in calls)
    proposal={'relation':'same','canonical_statement':'Full proposed wording'}
    I.review_pair(db,None,None,a,b,'policy',call,None,agent_name='belief_engine::merge_check',extra={'proposal':proposal})
    assert sum(name.endswith('evidence_page') for name,_ in calls)==before
    assert calls[-1][0]=='belief_engine::merge_check'
    assert calls[-1][1]['proposal']==proposal and calls[-1][1]['page_reviews']
