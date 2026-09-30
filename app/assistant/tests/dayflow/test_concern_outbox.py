"""The work attached to a concern reports to it and to the brain, and survives delivery failures.

Owner, 2026-09-30: the concern does not become the work; the work is attached to it, so the concern
shows how it is progressing. Every change reaches the concern through a receipt committed with the
change: attached, each finalizer judgment, the ending. Judgments and endings also reach the brain.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from work_objects.store import WorkStore
from app.assistant.subconscious import brain_inbox, concern_store, persist, concern_feedback, context_builder
from app.assistant.tests.dayflow.test_concern_propagation import _register, _CID
from belief_engine.intake.store import sqlite_file


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = _register(tmp_path)
    monkeypatch.setattr(concern_store, '_connect', path.connect)
    inbox = sqlite_file(tmp_path / 'inbox.db')
    monkeypatch.setattr(brain_inbox, '_connect', inbox)
    store = WorkStore(str(tmp_path/'work.db'))
    yield store, path, inbox
    store.close()


def graph(store, refs=None):
    wo = store.apply('create_work_object', {'title':'Discuss maintenance',
        'constraints':{'concern_refs': refs if refs is not None else [_CID],
                       'objective': 'Settle the maintenance question'}})
    store.apply('add_node', {'work_id':wo.id,'id':'main','type':'subtask',
        'title':'Ask about maintenance', 'parent_id':wo.goal_node_id,'status':'dispatched'})
    store.apply('record_result', {'work_id':wo.id,'node_id':'main','expected_dispatch_epoch':0,
        'evidence_id':'reply','title':'User reply','answer':'User will handle this','status':'done',
        'user_reply':{'ticket_id':'ticket',
        'question':'Arrange maintenance?', 'user_text':'I have arranged it. Stop asking.',
        'response_details':{'meaning':'acknowledge','scope':'Arrange maintenance'}}}, actor='ask')
    return wo.id


def finish(store, wid, verdict='achieved'):
    return store.apply('finalize_task', {'work_id':wid,'node_id':'main','expected_dispatch_epoch':0,
        'finalizer':{'verdict':verdict,'outcome':'User has arranged maintenance; no further contact.',
                     'next_step':''}}, actor='finalizer')


def work_events(inbox):
    brain_inbox.ensure_schema(inbox)
    with inbox(False) as c:
        return [dict(r) for r in c.execute("SELECT * FROM brain_events WHERE source='work' ORDER BY id")]


def test_attaching_work_marks_the_concern_in_progress(setup):
    store,path,inbox=setup
    wid=graph(store)
    assert [r['outcome'] for r in store.pending_concern_feedback()]==['attached']
    assert concern_feedback.recover_pending_concern_feedback(store)==1
    reg=path.read()
    assert reg['active']==[]
    concern=reg['addressing'][0]
    work=concern['attached_work'][wid]
    assert (work['status'], work['objective'], work['judgments'])==('active','Settle the maintenance question',[])
    assert f'WORK ATTACHED {wid}' in concern['reinforcement_notes']
    assert work_events(inbox)==[]                  # an attachment is a record, not news for the brain


def test_every_judgment_and_the_ending_reach_the_concern_and_the_brain(setup):
    store,path,inbox=setup
    wid=graph(store)
    assert finish(store,wid).status=='done'
    assert [r['outcome'] for r in store.pending_concern_feedback()]==['attached','judged','done']
    assert concern_feedback.recover_pending_concern_feedback(store)==3
    assert store.pending_concern_feedback()==[]
    reg=path.read()
    concern=reg['active'][0]                       # no work in progress: the brain decides the rest
    assert reg['addressing']==reg['resolved']==[]
    work=concern['attached_work'][wid]
    assert work['status']=='done' and work['ended']['outcome']=='done'
    [judgment]=work['judgments']
    assert judgment['verdict']=='achieved' and 'no further contact' in judgment['outcome']
    assert judgment['replies'][0]['user_text']=='I have arranged it. Stop asking.'
    judged, ended = work_events(inbox)
    for e in (judged, ended):
        assert (e['gate_status'], e['route'], json.loads(e['concern_ids']))==('routed','concern',[_CID])
    assert 'I have arranged it. Stop asking.' in judged['text'] and 'achieved' in judged['text']
    assert 'ended: done' in ended['text']
    for view in (context_builder._render_concern_summary(concern,status='active'),
                 context_builder._render_closed_concern(concern,status='dormant',why='user ownership')):
        assert 'I have arranged it. Stop asking.' in view
        assert 'no further contact' in view
        assert 'acknowledge' in view


def test_a_judgment_that_did_not_achieve_the_goal_reaches_the_brain_too(setup):
    store,path,inbox=setup
    wid=graph(store)
    finish(store,wid,verdict='retry')
    concern_feedback.recover_pending_concern_feedback(store)
    [event]=work_events(inbox)
    assert 'retry' in event['text']
    assert path.read()['addressing'][0]['attached_work'][wid]['status']=='active'


def test_revising_work_to_cite_a_concern_attaches_it(setup):
    store,path,inbox=setup
    wid=graph(store,refs=[])
    assert store.pending_concern_feedback()==[]
    wo=store.load(wid)
    store.apply('revise_goal', {'work_id':wid,'expected_updated_at':wo.updated_at,'objective':'Settle it',
        'content':'Settle it','constraints':{**wo.constraints,'concern_refs':[_CID]}}, actor='steward')
    assert [r['outcome'] for r in store.pending_concern_feedback()]==['attached']


def test_failure_survives_reopen_and_retries_without_repeating_register_write(setup,monkeypatch):
    store,path,inbox=setup
    wid=graph(store); finish(store,wid)
    monkeypatch.setattr(store,'acknowledge_concern_feedback',Mock(side_effect=OSError('crash before ack')))
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    # New connection proves recovery uses persistent state, not a Python callback.
    reopened=WorkStore(store.path)
    try:
        assert concern_feedback.recover_pending_concern_feedback(reopened)==3
        assert reopened.pending_concern_feedback()==[]
        concern=path.read()['active'][0]
        assert concern['reinforcement_notes'].count(f'WORK ATTACHED {wid}')==1
        assert len(concern['attached_work'][wid]['judgments'])==1
        assert len(work_events(inbox))==2
    finally:
        reopened.close()


def test_register_failure_does_not_lose_completion(setup,monkeypatch):
    store,path,inbox=setup
    wid=graph(store); finish(store,wid)
    real=persist._save_register
    monkeypatch.setattr(persist,'_save_register',Mock(side_effect=OSError('disk unavailable')))
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    assert store.load(wid).status=='done'
    assert len(store.pending_concern_feedback())==3
    monkeypatch.setattr(persist,'_save_register',real)
    assert concern_feedback.recover_pending_concern_feedback(store)==3


def test_unresolved_ref_retains_receipt_and_resolved_ref_is_idempotent(setup):
    store,path,inbox=setup
    wid=graph(store,[_CID,'concern:missingref']); finish(store,wid)
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    first=path.read()
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    assert path.read()==first
    assert len(store.pending_concern_feedback())==3


def test_failed_graph_transaction_has_no_receipt(setup,monkeypatch):
    store,path,inbox=setup
    wid=graph(store)
    concern_feedback.recover_pending_concern_feedback(store)
    real=store._queue_concern_feedback
    def fail(*args):
        real(*args)
        raise ValueError('transaction failed')
    monkeypatch.setattr(store,'_queue_concern_feedback',fail)
    with pytest.raises(ValueError): finish(store,wid)
    assert store.load(wid).status=='active'
    assert store.pending_concern_feedback()==[]


def test_finalizer_delivers_after_commit(setup,monkeypatch):
    store,path,inbox=setup
    wid=graph(store)
    from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
    from app.assistant.dayflow_orchestrator import work_store
    monkeypatch.setattr(work_store,'get_dayflow_work_store',lambda:store)
    wo=store.load(wid)
    WorkFinalizerNode._apply(SimpleNamespace(name='test'),wo,wo.nodes['main'],
        {'verdict':'achieved','outcome':'User arranged it.'})
    assert store.load(wid).status=='done'
    assert store.pending_concern_feedback()==[]
    assert path.read()['active'][0]['attached_work'][wid]['status']=='done'
    assert len(work_events(inbox))==2


def test_handling_history_survives_journal_trimming(setup):
    store,path,inbox=setup
    wid=graph(store); finish(store,wid)
    concern_feedback.recover_pending_concern_feedback(store)
    c=path.read()['active'][0]
    c['reinforcement_notes']+='\n'+'\n'.join(f'observation {i}' for i in range(30))
    persist._trim_journal(c)
    assert 'I have arranged it.' not in c['reinforcement_notes']
    assert 'I have arranged it.' in context_builder._render_concern_summary(c,status='active')


def test_existing_concerns_get_their_attached_work_once(tmp_path):
    path=_register(tmp_path)
    reg=path.read()
    c=reg['active'].pop()
    c['work_outcomes']={'work_old':{'work_id':'work_old','outcome':'done','recorded_at':'2026-09-01T00:00:00+00:00',
        'context':{'title':'Old attempt','terminal':{'reason':'done then'}},'user_response':{'user_text':'fine'}}}
    c['work_outcome_receipts']=['r1']
    reg['addressing'].append(c)                    # the old meaning: work had finished
    path.write(reg)
    store=WorkStore(str(tmp_path/'work.db'))
    try:
        wid=graph(store)
        loads=[]
        load=lambda: loads.append(1) or [store.load(wid)]
        assert persist.rederive_attached_work(load,connect=path.connect)==[]   # still in progress
        reg=path.read()
        concern=reg['addressing'][0]               # the live work keeps it in progress
        assert set(concern['attached_work'])=={wid,'work_old'}
        assert concern['attached_work'][wid]['status']=='active'
        assert concern['attached_work']['work_old']['ended']['owner_reply']=={'user_text':'fine'}
        assert 'work_outcomes' not in concern and concern['work_receipts']==['r1']
        assert persist.rederive_attached_work(load,connect=path.connect)==[]
        assert loads==[1]                          # nothing left to move: the store is not read
    finally:
        store.close()


def test_a_concern_whose_work_already_ended_goes_back_to_the_brain(tmp_path,monkeypatch):
    path=_register(tmp_path)
    reg=path.read()
    c=reg['active'].pop()
    reg['addressing'].append(c)
    path.write(reg)
    inbox=sqlite_file(tmp_path/'inbox.db')
    monkeypatch.setattr(brain_inbox,'_connect',inbox)
    store=WorkStore(str(tmp_path/'work.db'))
    try:
        wid=graph(store); finish(store,wid)
        reopened=persist.rederive_attached_work(lambda:[store.load(wid)],connect=path.connect)
        assert [r['concern_id'] for r in reopened]==[_CID]
        reg=path.read()
        assert reg['addressing']==[] and reg['active'][0]['attached_work'][wid]['ended']['outcome']=='done'
        assert concern_feedback.report_earlier_endings(reopened)==1
        assert concern_feedback.report_earlier_endings(reopened)==0
        [event]=work_events(inbox)
        assert 'ended: done' in event['text'] and json.loads(event['concern_ids'])==[_CID]
    finally:
        store.close()


def test_review_date_resets_age_pressure_without_faking_resolution():
    now=datetime.now(timezone.utc)
    c={'concern_id':_CID,'addressing_since_utc':(now-timedelta(days=10)).isoformat(),
       'addressing_reviewed_at_utc':(now-timedelta(days=1)).isoformat()}
    assert persist.compute_pressure({'addressing':[c]},now_utc=now)['addressing_stale']==[]
    assert persist.compute_pressure({'addressing':[c]},now_utc=now+timedelta(days=4))['addressing_stale']==[c]


def test_keep_tracking_persists_review_without_recontact_or_resolution(tmp_path):
    path=_register(tmp_path)
    reg=path.read()
    c=reg['active'].pop()
    since=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
    c['addressing_since_utc']=since
    reg['addressing'].append(c)
    path.write(reg)
    persist.apply_noticer_output({'concern_dispositions':[{'concern_id':_CID,
        'action':'keep_active','reason':'User owns the plan; no new evidence.'}]},
        connect=path.connect,tick_log_path=tmp_path/'ticks.jsonl')
    reg=path.read()
    assert reg['addressing'][0]['addressing_since_utc']==since
    assert persist.compute_pressure(reg)['addressing_stale']==[]
    assert reg['active']==reg['resolved']==[]


def test_noticer_prompt_renders_handling_policy():
    from app.assistant.dayflow_orchestrator import work_context
    prompt=work_context._ENV.get_template('subconscious/noticer/prompts/system.j2').render(
        resource_user_data={'first_name':'User'},resource_subconscious_noticer_house_rules='')
    assert 'four days in addressing alone is not new evidence' in prompt
    assert 'attached_work' in prompt
