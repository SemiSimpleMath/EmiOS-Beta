import json
import pytest
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.utils.path_utils import get_app_root
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


def test_pages_cap_fragment_count_not_just_chars():
    from belief_engine.matching.context import pages as P
    tiny = [{'id': i, 'text': 'x'} for i in range(30)]
    # Char bounding alone puts all 30 on one page; the count cap is what splits them.
    assert len(list(P(tiny, max_chars=10**6))) == 1
    batches = list(P(tiny, max_chars=10**6, max_items=12))
    assert [len(b) for b in batches] == [12, 12, 6]
    assert [r for b in batches for r in b] == tiny          # nothing dropped or reordered


def test_evidence_pages_stay_within_the_fragment_cap():
    a, b = packets()
    for page in I.pages(I.fragments_for(a, b), max_chars=36000,
                        max_items=I.MAX_FRAGMENTS_PER_PAGE):
        assert len(page) <= I.MAX_FRAGMENTS_PER_PAGE


RULES = AgentRegistry()._load_validator(
    get_app_root() / 'assistant' / 'agents' / 'belief_engine' / 'evidence_page' / 'validate.py')
PAGE = {'beliefs': {}, 'fragments': [{'fragment_id': 'a:evidence:0:0'}, {'fragment_id': 'a:evidence:0:12000'},
                                     {'fragment_id': 'b:evidence:0:0'}]}


def test_the_page_rules_require_one_finding_per_fragment_coverage_first():
    """Moved from investigation.page_problem (2026-11-15); a single dropped id once aborted the whole pass."""
    def answer(*pairs):
        return {'findings': [{'fragment_id': i, 'findings': f} for i, f in pairs]}
    assert RULES.validate(answer(('a:evidence:0:0', 'read'), ('a:evidence:0:12000', 'read'),
                                 ('b:evidence:0:0', 'read')), PAGE) == []
    bad = RULES.validate(answer(('a:evidence:0:0', 'read'), ('a:evidence:0:0', 'read'), ('x:1', 'read'),
                                ('b:evidence:0:0', ' ')), PAGE)
    assert [(p.item, p.value, p.structural) for p in bad] == [
        ('', 'a:evidence:0:12000', True), ('a:evidence:0:0', None, True), ('', 'x:1', True)]   # empty one not yet
    empty = RULES.validate(answer(('a:evidence:0:0', 'read'), ('a:evidence:0:12000', ''),
                                  ('b:evidence:0:0', 'read')), PAGE)
    assert [(p.item, p.at, p.structural) for p in empty] == [('a:evidence:0:12000', 'findings', False)]
    assert RULES.ITEM_KEYS == {'findings': 'fragment_id'}


def test_each_page_is_reviewed_by_one_call_and_a_stored_receipt_is_checked_by_the_same_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(I, 'DIRECT_LIMIT', 10000)
    payloads = []

    def call(factory, name, payload, scope, form):
        if not name.endswith('evidence_page'):
            return {'relation': 'unresolved'}
        payloads.append(payload)
        return {'findings': [{'fragment_id': f['fragment_id'], 'findings': 'read'} for f in payload['fragments']]}

    a, b = packets()
    db = tmp_path / 'copy.db'
    I.review_pair(db, None, None, a, b, 'policy', call, None)
    assert payloads and all(set(p) == {'beliefs', 'fragments'} for p in payloads)
    with I.connection(db) as conn:
        conn.execute("UPDATE belief_match_pages SET result_json='{\"findings\": []}'")
    with pytest.raises(ValueError, match='Stored evidence-page receipt is invalid: findings: no finding'):
        I.review_pair(db, None, None, a, b, 'policy', call, None)
