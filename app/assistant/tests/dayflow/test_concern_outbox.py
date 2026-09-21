"""Closed work survives delivery failures and remains visible to the noticer."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from work_objects.store import WorkStore
from app.assistant.subconscious import persist, answer_capture, concern_feedback, context_builder
from app.assistant.tests.dayflow.test_concern_propagation import _register, _CID


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = _register(tmp_path)
    real = persist.apply_work_outcome
    monkeypatch.setattr(persist, 'apply_work_outcome', lambda *a, **kw: real(*a, **kw, register_path=path))
    trigger = Mock()
    monkeypatch.setattr(answer_capture, 'trigger_noticer', trigger)
    store = WorkStore(str(tmp_path/'work.db'))
    yield store, path, trigger
    store.close()


def graph(store, refs=None):
    wo = store.apply('create_work_object', {'title':'Discuss maintenance',
        'constraints':{'concern_refs': refs if refs is not None else [_CID]}})
    store.apply('add_node', {'work_id':wo.id,'id':'main','type':'subtask',
        'title':'Ask about maintenance', 'parent_id':wo.goal_node_id,'status':'dispatched'})
    store.apply('record_result', {'work_id':wo.id,'node_id':'main','expected_dispatch_epoch':0,
        'evidence_id':'reply','title':'User reply','answer':'User will handle this','status':'done',
        'user_reply':{'ticket_id':'ticket',
        'question':'Arrange maintenance?', 'user_text':'I have arranged it. Stop asking.',
        'response_details':{'meaning':'acknowledge','scope':'Arrange maintenance'}}}, actor='ask')
    return wo.id


def finish(store, wid):
    return store.apply('finalize_task', {'work_id':wid,'node_id':'main','expected_dispatch_epoch':0,
        'finalizer':{'verdict':'achieved','outcome':'User has arranged maintenance; no further contact.',
                     'next_step':''}}, actor='finalizer')


def test_automatic_rollup_queues_then_delivers_visible_history(setup):
    store,path,trigger=setup
    wid=graph(store)
    assert store.pending_concern_feedback()==[]
    assert finish(store,wid).status=='done'
    assert len(store.pending_concern_feedback())==1
    trigger.assert_not_called()  # Generic store has no application side effects.
    assert concern_feedback.recover_pending_concern_feedback(store)==1
    assert store.pending_concern_feedback()==[]
    concern=json.loads(path.read_text())['addressing'][0]
    assert json.loads(path.read_text())['resolved']==[]
    for view in (context_builder._render_concern_summary(concern,status='addressing'),
                 context_builder._render_closed_concern(concern,status='dormant',why='user ownership')):
        assert 'I have arranged it. Stop asking.' in view
        assert 'no further contact' in view
        assert 'acknowledge' in view
    concern_feedback.propagate_work_outcome(store,wid,'done')
    trigger.assert_called_once()


def test_failure_survives_reopen_and_retries_without_repeating_register_write(setup,monkeypatch):
    store,path,trigger=setup
    wid=graph(store); finish(store,wid)
    ack=store.acknowledge_concern_feedback
    monkeypatch.setattr(store,'acknowledge_concern_feedback',Mock(side_effect=OSError('crash before ack')))
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    first=path.read_text()
    # New connection proves recovery uses persistent state, not a Python callback.
    reopened=WorkStore(store.path)
    try:
        assert concern_feedback.recover_pending_concern_feedback(reopened)==1
        assert reopened.pending_concern_feedback()==[]
        assert path.read_text()==first
    finally:
        reopened.close()


def test_register_failure_does_not_lose_completion(setup,monkeypatch):
    store,path,trigger=setup
    wid=graph(store); finish(store,wid)
    real=persist._save_register
    monkeypatch.setattr(persist,'_save_register',Mock(side_effect=OSError('disk unavailable')))
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    assert store.load(wid).status=='done'
    assert len(store.pending_concern_feedback())==1
    monkeypatch.setattr(persist,'_save_register',real)
    assert concern_feedback.recover_pending_concern_feedback(store)==1


def test_unresolved_ref_retains_receipt_and_resolved_ref_is_idempotent(setup):
    store,path,trigger=setup
    wid=graph(store,[_CID,'concern:missingref']); finish(store,wid)
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    first=path.read_text()
    assert concern_feedback.recover_pending_concern_feedback(store)==0
    assert path.read_text()==first
    assert len(store.pending_concern_feedback())==1


def test_failed_graph_transaction_has_no_receipt(setup,monkeypatch):
    store,path,trigger=setup
    wid=graph(store)
    real=store._queue_concern_feedback
    def fail(*args):
        real(*args)
        raise ValueError('transaction failed')
    monkeypatch.setattr(store,'_queue_concern_feedback',fail)
    with pytest.raises(ValueError): finish(store,wid)
    assert store.load(wid).status=='active'
    assert store.pending_concern_feedback()==[]


def test_finalizer_delivers_after_commit(setup,monkeypatch):
    store,path,trigger=setup
    wid=graph(store)
    from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
    from app.assistant.dayflow_orchestrator import work_store
    monkeypatch.setattr(work_store,'get_dayflow_work_store',lambda:store)
    wo=store.load(wid)
    WorkFinalizerNode._apply(SimpleNamespace(name='test'),wo,wo.nodes['main'],
        {'verdict':'achieved','outcome':'User arranged it.'})
    assert store.load(wid).status=='done'
    assert store.pending_concern_feedback()==[]
    assert json.loads(path.read_text())['addressing']
    trigger.assert_called_once()


def test_handling_history_survives_journal_trimming(setup):
    store,path,trigger=setup
    wid=graph(store); finish(store,wid)
    concern_feedback.recover_pending_concern_feedback(store)
    c=json.loads(path.read_text())['addressing'][0]
    c['reinforcement_notes']+='\n'+'\n'.join(f'observation {i}' for i in range(30))
    persist._trim_journal(c)
    assert 'I have arranged it.' not in c['reinforcement_notes']
    assert 'I have arranged it.' in context_builder._render_concern_summary(c,status='addressing')


def test_review_date_resets_age_pressure_without_faking_resolution():
    now=datetime.now(timezone.utc)
    c={'concern_id':_CID,'addressing_since_utc':(now-timedelta(days=10)).isoformat(),
       'addressing_reviewed_at_utc':(now-timedelta(days=1)).isoformat()}
    assert persist.compute_pressure({'addressing':[c]},now_utc=now)['addressing_stale']==[]
    assert persist.compute_pressure({'addressing':[c]},now_utc=now+timedelta(days=4))['addressing_stale']==[c]


def test_keep_tracking_persists_review_without_recontact_or_resolution(tmp_path):
    path=_register(tmp_path)
    reg=json.loads(path.read_text())
    c=reg['active'].pop()
    since=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
    c['addressing_since_utc']=since
    reg['addressing'].append(c)
    path.write_text(json.dumps(reg))
    persist.apply_noticer_output({'concern_dispositions':[{'concern_id':_CID,
        'action':'keep_active','reason':'User owns the plan; no new evidence.'}]},
        register_path=path,tick_log_path=tmp_path/'ticks.jsonl')
    reg=json.loads(path.read_text())
    assert reg['addressing'][0]['addressing_since_utc']==since
    assert persist.compute_pressure(reg)['addressing_stale']==[]
    assert reg['active']==reg['resolved']==[]


def test_noticer_prompt_renders_handling_policy():
    from app.assistant.dayflow_orchestrator import work_context
    prompt=work_context._ENV.get_template('subconscious/noticer/prompts/system.j2').render(
        resource_user_data={'first_name':'User'},resource_subconscious_noticer_house_rules='')
    assert 'four days in addressing alone is not new evidence' in prompt
    assert 'handling_history' in prompt
