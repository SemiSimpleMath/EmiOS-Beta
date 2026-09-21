"""Closure decisions survive graph-write failure and fence execution until recovery."""
from pathlib import Path
from unittest.mock import Mock
import pytest

from work_objects.store import WorkStore
from work_objects.execution_store import ExecutionBlocked
from app.assistant.manager_runtime.execution import Owner, ExecutionCancelled
from app.assistant.dayflow_orchestrator.work_persist import persist_steward_output, recover_pending_work_closures
from app.assistant.subconscious import concern_feedback
from app.assistant.tests.dayflow.conftest import FakeBlackboard


@pytest.fixture
def store(tmp_path):
    result = WorkStore(str(tmp_path/'closures.db'))
    yield result
    result.close()


def work(store, title='Work'):
    wo = store.apply('create_work_object', {'title': title, 'goal_content': title,
        'satisfied_when_kind': 'all_owned_children_done'})
    nid = 'main' if title == 'Work' else 'main-' + title
    store.apply('add_node', {'work_id': wo.id, 'id': nid, 'type': 'subtask',
        'parent_id': wo.goal_node_id, 'title': 'Do the work'})
    store.apply('set_status', {'work_id': wo.id, 'node_id': nid, 'status': 'actionable'})
    return wo.id


def fail_terminal_write(store):
    with store._conn:
        store._conn.execute("""CREATE TRIGGER reject_terminal BEFORE INSERT ON work_objects
            WHEN NEW.status IN ('done','abandoned') BEGIN SELECT RAISE(ABORT,'simulated closure failure'); END""")


def request(wid, status='abandoned'):
    return {'work_id': wid, 'status': status, 'reason': 'User declined this work'}


@pytest.mark.parametrize('key,status', [('abandon_work_ids','abandoned'), ('complete_work_ids','done')])
def test_failed_terminal_commit_stops_pass_and_survives_reopen(store, tmp_path, monkeypatch, key, status):
    wid = work(store)
    feedback = Mock()
    monkeypatch.setattr(concern_feedback, 'propagate_work_outcome', feedback)
    fail_terminal_write(store)
    with pytest.raises(Exception, match='simulated closure failure'):
        persist_steward_output(store, {key:[wid], 'new_or_changed':[{'objective':'Must not be created'}]})
    feedback.assert_not_called()
    assert len(store.list_work_objects()) == 1
    wo = store.load(wid)
    assert wo.status == 'active'
    assert wo.constraints['pending_work_closure']['status'] == status
    assert not wo.ready_nodes()
    other = WorkStore(str(tmp_path/'closures.db'))
    try:
        with pytest.raises(ExecutionBlocked, match='closure is pending'):
            other.apply('claim_task', {'work_id':wid, 'node_id':'main'})
        with other._conn:
            other._conn.execute('DROP TRIGGER reject_terminal')
        result = recover_pending_work_closures(other)
        assert result['completed' if status=='done' else 'abandoned'] == [wid]
        assert other.load(wid).status == status
        assert other.pending_work_closures() == []
        assert not other.load(wid).ready_nodes()
        feedback.assert_called_once_with(other, wid, status)
        recover_pending_work_closures(other)
        assert feedback.call_count == 1
    finally:
        other.close()


def test_pending_intent_blocks_running_worker_and_future_tool_call(store):
    wid = work(store)
    store.apply('claim_task', {'work_id':wid,'node_id':'main'})
    owner = Owner(store,wid,'main',1)
    store.start_execution(owner)
    # An already-admitted external call remains tracked; cancelling cannot unsend it.
    store.admit_execution_call(owner,'existing','fake_email',True)
    store.queue_work_closures([request(wid)])
    assert store.execution_revoked(owner)
    with pytest.raises(ExecutionCancelled):
        store.admit_execution_call(owner,'new','fake_email',True)
    from work_objects.runtime import set_work_context, reset_work_context
    with pytest.raises(ExecutionCancelled):
        set_work_context(store,wid,'main','worker')
    with pytest.raises(ExecutionBlocked):
        store.apply('edit_node',{'work_id':wid,'node_id':'main','title':'Late edit'})
    assert store.load(wid).nodes['main'].title == 'Do the work'
    assert [c['id'] for c in store.execution_status(wid)['calls']] == ['existing']


def test_all_intents_saved_before_first_closure_attempt(store, monkeypatch):
    ids = [work(store,'One'),work(store,'Two')]
    fail_terminal_write(store)
    with pytest.raises(Exception,match='simulated closure failure'):
        persist_steward_output(store,{'abandon_work_ids':ids})
    assert {r['work_id'] for r in store.pending_work_closures()} == set(ids)
    for wid in ids:
        assert not store.load(wid).ready_nodes()


def test_intent_batch_is_atomic_when_later_id_is_invalid(store):
    wid = work(store)
    with pytest.raises(Exception):
        store.queue_work_closures([request(wid),request('missing-work')])
    assert store.pending_work_closures() == []
    assert store.load(wid).ready_nodes()


def test_conflicting_complete_and_abandon_is_rejected_without_partial_intent(store):
    wid = work(store)
    with pytest.raises(ValueError,match='conflicting'):
        persist_steward_output(store,{'complete_work_ids':[wid],'abandon_work_ids':[wid]})
    assert store.pending_work_closures() == []
    assert store.load(wid).status == 'active'


def test_feedback_failure_does_not_undo_closure_or_skip_other_closures(store, monkeypatch, caplog):
    ids = [work(store,'One'), work(store,'Two')]
    monkeypatch.setattr(concern_feedback,'propagate_work_outcome', Mock(side_effect=OSError('feedback offline')))
    result = persist_steward_output(store,{'abandon_work_ids':ids})
    assert set(result['abandoned']) == set(ids)
    assert all(store.load(wid).status=='abandoned' for wid in ids)
    assert store.pending_work_closures() == []


def test_successful_closure_stays_closed_if_later_closure_fails(store, monkeypatch):
    ids = sorted([work(store,'One'),work(store,'Two')])
    apply = store.apply
    def failing(op,data,**kwargs):
        if op=='set_work_status' and data['work_id']==ids[1]:
            raise OSError('Second closure unavailable')
        return apply(op,data,**kwargs)
    monkeypatch.setattr(store,'apply',failing)
    with pytest.raises(OSError):
        persist_steward_output(store,{'abandon_work_ids':ids})
    assert store.load(ids[0]).status=='abandoned'
    assert [r['work_id'] for r in store.pending_work_closures()]==[ids[1]]


def test_evaluator_prep_recovers_before_rendering_portfolio(store,monkeypatch):
    from app.assistant.control_nodes.strategic_planner_wo_prep_node import StrategicPlannerWoPrepNode
    from app.assistant.dayflow_orchestrator import work_store
    wid=work(store)
    store.queue_work_closures([request(wid)])
    monkeypatch.setattr(work_store,'get_dayflow_work_store',lambda:store)
    monkeypatch.setattr(StrategicPlannerWoPrepNode,'_build_situational_context',lambda self:None)
    bb=FakeBlackboard()
    node=StrategicPlannerWoPrepNode(name='prep',blackboard=bb,agent_registry={},tool_registry={})
    node.action_handler(None)
    assert store.load(wid).status=='abandoned'
    assert store.pending_work_closures()==[]
    assert wid not in bb.get_state_value('work_portfolio')


def test_failed_recovery_prevents_new_evaluation(store,monkeypatch):
    from app.assistant.control_nodes.strategic_planner_wo_prep_node import StrategicPlannerWoPrepNode
    from app.assistant.dayflow_orchestrator import work_store
    wid=work(store)
    store.queue_work_closures([request(wid)])
    fail_terminal_write(store)
    monkeypatch.setattr(work_store,'get_dayflow_work_store',lambda:store)
    bb=FakeBlackboard()
    node=StrategicPlannerWoPrepNode(name='prep',blackboard=bb,agent_registry={},tool_registry={})
    with pytest.raises(Exception,match='simulated closure failure'):
        node.action_handler(None)
    assert bb.get_state_value('last_agent') is None
    assert bb.get_state_value('work_portfolio') is None


def test_same_pass_source_handoff_and_closure_preserves_user_context(store):
    wid = work(store)
    item = {'metadata': {'item_id':'reply', 'summary':'User says this was already handled',
                         'pod_id':'datapod:reply'}}
    result = persist_steward_output(store, {'complete_work_ids':[wid], 'new_or_changed':[
        {'work_id':wid,'objective':'Work already handled','based_on':['reply']}]}, admitted_artifacts=[item])
    wo = store.load(wid)
    assert wo.status == 'done'
    assert any(s['pod_id'] == 'datapod:reply' for s in wo.constraints['source_intake'])
    assert result['changed_records'] == [{'work_id':wid,'based_on':['reply']}]
    assert store.pending_work_closures() == []
