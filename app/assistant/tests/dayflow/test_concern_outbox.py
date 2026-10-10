"""Work made for a concern is attached to it as a reference; its judgments and ending reach the brain; delivery
survives failures.

Owner, 2026-09-30: the concern does not become the work; the work is attached to it. Owner, 2026-10-09: "attached work
should just be a reference to the work object. then its rendered. that way if work object is updated then the
attached work is automatically updated." The concern keeps the work's id and when it was attached; readers render the
work live (concern_view.attempts). Attaching or ending work moves the concern nowhere, and a decline closes nothing:
only the brain, or the concern's expiry, closes it. Each judgment and the ending reach the brain inbox, pending for
the gate, naming what the work was made for.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from work_objects.store import WorkStore
from app.assistant.subconscious import brain_inbox, concern_feedback, concern_store, concern_view, persist
from app.assistant.tests.dayflow.test_concern_propagation import _register, _CID
from belief_engine.intake.store import sqlite_file


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = _register(tmp_path)
    monkeypatch.setattr(concern_store, '_connect', path.connect)
    inbox = sqlite_file(tmp_path / 'inbox.db')
    monkeypatch.setattr(brain_inbox, '_connect', inbox)
    store = WorkStore(str(tmp_path/'work.db'))
    from app.assistant.dayflow_orchestrator import work_store
    monkeypatch.setattr(work_store, 'get_dayflow_work_store', lambda: store)
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


def test_attaching_work_keeps_a_reference_and_moves_nothing(setup):
    store,path,inbox=setup
    wid=graph(store)
    assert [r['outcome'] for r in store.pending_concern_feedback()]==['attached']
    assert concern_feedback.recover_pending_concern_feedback(store)==1
    reg=path.read()
    assert reg['addressing']==[], 'attaching work moves the concern nowhere'
    concern=reg['active'][0]
    assert set(concern['attached_work'][wid])=={'work_id','attached_at'}, 'a reference, nothing copied'
    [work]=concern_view.attempts(concern)
    assert (work['status'], work['objective'], work['judgments'])==('active','Settle the maintenance question',[])
    assert 'WORK ATTACHED' not in (concern.get('reinforcement_notes') or '')   # the notes are the brain's
    assert work_events(inbox)==[]                  # an attachment is a record, not news for the brain


def test_every_judgment_and_the_ending_reach_the_concern_and_the_brain(setup):
    store,path,inbox=setup
    wid=graph(store)
    assert finish(store,wid).status=='done'
    assert [r['outcome'] for r in store.pending_concern_feedback()]==['attached','judged','done']
    assert concern_feedback.recover_pending_concern_feedback(store)==3
    assert store.pending_concern_feedback()==[]
    reg=path.read()
    concern=reg['active'][0]                       # the brain decides whether the concern is settled
    assert reg['addressing']==reg['resolved']==reg['dormant']==[]
    assert set(concern['attached_work'][wid])=={'work_id','attached_at'}, 'nothing written back on the concern'
    [work]=concern_view.attempts(concern)          # read live from the work
    assert work['status']=='done' and work['ended']['outcome']=='done'
    [judgment]=work['judgments']
    assert 'no further contact' in judgment['outcome'] and 'verdict' not in judgment
    assert 'said: "I have arranged it. Stop asking."' in judgment['replies'][0]
    judged, ended = work_events(inbox)
    for e in (judged, ended):
        assert (e['gate_status'], e['route']) == ('pending', None)    # the gate routes it, like chat
        assert 'Objective: Settle the maintenance question' in e['text']
        assert f'Made for the concerns: "{concern["title"]}"' in e['text']
    assert 'I have arranged it. Stop asking.' in judged['text']
    assert 'User has arranged maintenance; no further contact.' in judged['text'] and 'achieved' not in judged['text']
    from app.assistant.utils.time_utils import local_time_text
    assert f"ended {local_time_text(work['ended']['at'])}: done" in ended['text'], 'the report carries its date'


def test_a_judgment_that_did_not_achieve_the_goal_reaches_the_brain_too(setup):
    store,path,inbox=setup
    wid=graph(store)
    finish(store,wid,verdict='retry')
    concern_feedback.recover_pending_concern_feedback(store)
    [event]=work_events(inbox)
    # The finalizer's account reaches the brain; its verdict stays with dayflow (2026-10-04).
    assert 'User has arranged maintenance; no further contact.' in event['text'] and 'retry' not in event['text']
    assert concern_view.attempts(path.read()['active'][0])[0]['status']=='active'


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
        assert list(concern['attached_work'])==[wid]
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
    wo=store.load(wid)
    WorkFinalizerNode._apply(SimpleNamespace(name='test'),wo,wo.nodes['main'],
        {'verdict':'achieved','outcome':'User arranged it.'})
    assert store.load(wid).status=='done'
    assert store.pending_concern_feedback()==[]
    assert concern_view.attempts(path.read()['active'][0])[0]['status']=='done'
    assert len(work_events(inbox))==2


def test_a_long_journal_is_kept_whole(setup):
    """The journal is kept whole (2026-10-08)."""
    store,path,inbox=setup
    wid=graph(store); finish(store,wid)
    concern_feedback.recover_pending_concern_feedback(store)
    c=path.read()['active'][0]
    for i in range(30):
        persist._journal_on(c,'2026-10-08T00:00:00+00:00',f'observation {i}')
    assert 'observation 0' in c['reinforcement_notes'] and 'archived' not in c['reinforcement_notes']


def test_a_declined_ask_closes_nothing(setup):
    """Until 2026-10-09 a decline button on a work's ticket parked the concern dormant: code closing a concern."""
    store,path,inbox=setup
    wid=graph(store)
    store.apply('set_work_status', {'work_id':wid,'status':'abandoned','reason':'The owner declined'})
    concern_feedback.recover_pending_concern_feedback(store)
    reg=path.read()
    assert [c['concern_id'] for c in reg['active']]==[_CID] and reg['dormant']==[]


def test_only_work_made_for_a_concern_reports_to_the_brain(setup):
    store,path,inbox=setup
    made_for=graph(store)
    routine=store.apply('create_work_object', {'title':'Evening walk reminder', 'constraints':{}}).id
    assert concern_feedback.reports_to_brain(made_for, store=store)
    assert not concern_feedback.reports_to_brain(routine, store=store)
