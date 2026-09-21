import threading
from contextvars import copy_context
import pytest
from work_objects.store import WorkStore
from work_objects.runtime import set_work_context, reset_work_context, get_work_context
from app.assistant.manager_runtime.execution import REGISTRY, Owner, ExecutionCancelled, tool_call, agent_activation, model_boundary


@pytest.fixture
def task(tmp_path):
    store = WorkStore(str(tmp_path/'work.db'))
    wo = store.apply('create_work_object', dict(title='test', goal_content='test', satisfied_when_kind='all_owned_children_done'))
    for nid in ('main','replacement'):
        store.apply('add_node', dict(work_id=wo.id,id=nid,type='subtask',parent_id=wo.goal_node_id,title=nid))
        store.apply('set_status', dict(work_id=wo.id,node_id=nid,status='actionable'))
    store.apply('claim_task', dict(work_id=wo.id,node_id='main'))
    owner = Owner(store,wo.id,'main',1)
    store.start_execution(owner)
    yield store,owner
    store.close()


def test_hung_call_remains_visible_and_blocks_replacement(task):
    store,owner=task
    entered,release=threading.Event(),threading.Event()
    errors=[]
    def run():
        try:
            with REGISTRY.span('attempt','worker',owner=owner):
                with tool_call('fake_device'):
                    entered.set()
                    assert release.wait(5)
                with pytest.raises(ExecutionCancelled):
                    with tool_call('second_action'):
                        pytest.fail('revoked call executed')
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.finish_execution(owner)
    thread=threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        store.revoke_execution(owner,'timeout')
        REGISTRY.cancel_owner(owner,'timeout')
        assert any(s['name']=='fake_device' and s['state']=='cancelling' for s in REGISTRY.snapshot()['active'])
        with pytest.raises(ValueError,match='prior execution'):
            store.apply('claim_task',dict(work_id=owner.work_id,node_id='replacement'))
    finally:
        release.set();thread.join(5)
    assert not errors
    assert not thread.is_alive()
    store.apply('claim_task',dict(work_id=owner.work_id,node_id='replacement'))


def test_unknown_external_outcome_survives_exit_and_new_connection(task):
    store,owner=task
    with REGISTRY.span('attempt','worker',owner=owner):
        with pytest.raises(OSError):
            with tool_call('fake_email'):
                raise OSError('connection lost after send')
    store.finish_execution(owner)
    other=WorkStore(store.path)
    try:
        with pytest.raises(ValueError,match='unknown'):
            other.apply('claim_task',dict(work_id=owner.work_id,node_id='replacement'))
        receipt=other.execution_status(owner.work_id)['calls'][0]
        assert receipt['state']=='unknown'
        with pytest.raises(ValueError,match='evidence'):
            other.resolve_execution_call(receipt['id'],evidence='')
        other.resolve_execution_call(receipt['id'],evidence='Provider operation lookup confirms message sent; no resend required.')
        other.apply('claim_task',dict(work_id=owner.work_id,node_id='replacement'))
    finally:
        other.close()


def test_helper_keeps_main_epoch_and_stale_writes_roll_back(task):
    store,owner=task
    token=set_work_context(store,owner.work_id,'main','worker')
    try:
        store.apply('add_node',dict(work_id=owner.work_id,id='helper',type='subtask',parent_id='main',title='notes'))
        inner=set_work_context(store,owner.work_id,'helper','child')
        try:
            assert get_work_context().owner == owner
            store.revoke_execution(owner,'timeout')
            with pytest.raises(ExecutionCancelled):
                store.apply('edit_node',dict(work_id=owner.work_id,node_id='helper',title='late'))
            assert store.load(owner.work_id).nodes['helper'].title=='notes'
        finally:
            reset_work_context(inner)
    finally:
        reset_work_context(token)


def test_cancelled_model_result_never_reconciles():
    reconciled=[]
    class Fake:
        name='fake'
        @model_boundary
        def call_llm(self):
            REGISTRY.cancel('parent')
            return 'response'
        @agent_activation
        def action_handler(self):
            result=self.call_llm()
            reconciled.append(result)
    with REGISTRY.span('manager','parent',span_id='parent'):
        with pytest.raises(ExecutionCancelled):
            Fake().action_handler()
        with pytest.raises(ExecutionCancelled):
            with REGISTRY.span('manager','new child'):
                pytest.fail('child ran')
    assert not reconciled
    assert not any(s['root_id']=='parent' for s in REGISTRY.snapshot()['active'])


def test_context_copy_tracks_child_after_parent_returns():
    entered,release=threading.Event(),threading.Event()
    def run():
        with REGISTRY.span('manager','child'):
            entered.set()
            assert release.wait(5)
    with REGISTRY.span('manager','parent',span_id='async-parent'):
        context=copy_context()
        thread=threading.Thread(target=context.run,args=(run,))
        thread.start()
        assert entered.wait(5)
    try:
        assert any(s['parent_id']=='async-parent' for s in REGISTRY.snapshot()['active'])
    finally:
        release.set();thread.join(5)


def test_restart_marks_unfinished_external_call_unknown(task,monkeypatch):
    store,owner=task
    store.admit_execution_call(owner,'crashed-call','fake_email',True)
    with store._lock,store._conn:
        store._conn.execute('UPDATE work_execution_attempts SET process_started=-1')
    with pytest.raises(ValueError,match='unknown'):
        store.apply('claim_task',dict(work_id=owner.work_id,node_id='replacement'))
    # Reconciliation in a rejected claim rolls back with the transaction; the
    # in-flight receipt still blocks and is reconciled by explicit maintenance.
    assert store.execution_status()['calls'][0]['state'] in {'in_flight','unknown'}


def test_plain_chat_has_no_work_database_calls():
    with REGISTRY.span('manager','chat'):
        with tool_call('fake_read'):
            assert REGISTRY.snapshot()['active'][-1]['owner'] is None


def test_independent_work_not_cross_cancelled(task,tmp_path):
    store,owner=task
    other=Owner(store,'unrelated','main',1)
    with REGISTRY.span('attempt','worker',owner=owner):
        REGISTRY.cancel_owner(other,'unrelated')
        REGISTRY.check()


def test_queued_descendant_keeps_attempt_owned_after_parent_exit(task):
    from app.assistant.manager_runtime.execution import bind_background
    store,owner=task
    with REGISTRY.span('attempt','parent',owner=owner):
        bound,cleanup=bind_background(lambda: 'done','queued child')
    REGISTRY.finish_owner(owner)
    assert store.execution_status()['attempts'][0]['state']=='running'
    with pytest.raises(ValueError,match='prior execution'):
        store.apply('claim_task',dict(work_id=owner.work_id,node_id='replacement'))
    assert bound()=='done'
    assert store.execution_status()['attempts'][0]['state']=='exited'
    cleanup()  # Idempotent cleanup after execution.


def test_cancelled_queued_descendant_never_runs(task):
    from app.assistant.manager_runtime.execution import bind_background
    store,owner=task
    ran=[]
    with REGISTRY.span('attempt','parent',owner=owner):
        bound,cleanup=bind_background(lambda: ran.append(True),'queued child')
        store.revoke_execution(owner,'stop')
    REGISTRY.finish_owner(owner)
    with pytest.raises(ExecutionCancelled):
        bound()
    assert not ran
    assert store.execution_status()['attempts'][0]['state']=='exited'


def test_abandonment_revokes_and_preserves_active_call(task):
    store,owner=task
    with REGISTRY.span('attempt','parent',owner=owner):
        with tool_call('blocking_fake'):
            store.apply('set_work_status',dict(work_id=owner.work_id,status='abandoned',reason='user stopped work'))
            assert store.execution_revoked(owner)
            assert any(s['name']=='blocking_fake' and s['state']=='cancelling' for s in REGISTRY.snapshot()['active'])
        with pytest.raises(ExecutionCancelled):
            with tool_call('next'):
                pytest.fail('ran after abandonment')


def test_restart_before_external_call_releases_barrier(task):
    store,owner=task
    with store._lock,store._conn:
        store._conn.execute('UPDATE work_execution_attempts SET process_started=-1')
    store.apply('claim_task',dict(work_id=owner.work_id,node_id='replacement'))
    assert store.execution_status()['attempts'][0]['state']=='interrupted'


def test_two_epoch_spans_do_not_overwrite_each_other(task):
    store,owner=task
    with REGISTRY.span('attempt','old',owner=owner) as old:
        with REGISTRY.span('attempt','new',owner=Owner(store,owner.work_id,'main',2)) as new:
            ids={s['id'] for s in REGISTRY.snapshot()['active']}
            assert old.id in ids and new.id in ids


def test_real_agent_override_calling_super_is_one_activation():
    from app.assistant.agent_classes.Agent import Agent
    class Parent(Agent):
        def __init__(self):
            self.name='override test'
        def action_handler(self,message):
            return [s for s in REGISTRY.snapshot()['active'] if s['kind']=='agent' and s['name']==self.name]
    class Child(Parent):
        def action_handler(self,message):
            return super().action_handler(message)
    assert len(Child().action_handler(None))==1


def test_migration_is_idempotent_and_preserves_graph(task):
    store,owner=task
    second=WorkStore(store.path)
    try:
        assert second.load(owner.work_id).nodes['main'].status=='dispatched'
        assert len(second.execution_status()['attempts'])==1
    finally:
        second.close()


def test_stale_main_epoch_cannot_be_adopted_by_helper(task):
    store,owner=task
    with REGISTRY.span('attempt','old',owner=owner):
        # Simulate an external transition; the current span retains epoch 1.
        with store._lock,store._conn:
            store._conn.execute("UPDATE nodes SET payload=json_set(payload,'$.dispatch_epoch',2) WHERE work_id=? AND id='main'",(owner.work_id,))
        with pytest.raises(ExecutionCancelled):
            set_work_context(store,owner.work_id,'main','worker')


def test_cancel_after_approval_prevents_actual_tool_call(monkeypatch):
    from app.assistant.control_nodes._tool_caller_util import _execute_tool
    from app.assistant.lib.tool_execution import tool_access_control as access, tool_approval as approval
    class Blackboard:
        def get_state_value(self,key,default=None):
            return default
    ran=[]
    class Tool:
        def execute(self,message):
            ran.append(True)
    monkeypatch.setattr(access,'check_tool_access',lambda **kw:(True,''))
    monkeypatch.setattr(access,'resolve_tool_min_authority',lambda *a:0)
    monkeypatch.setattr(approval,'compute_approval_reasons',lambda **kw:['test'])
    def approve(**kw):
        REGISTRY.cancel('approval-parent')
        return True,None,None
    monkeypatch.setattr(approval,'request_approval',approve)
    with REGISTRY.span('manager','approval',span_id='approval-parent'):
        with pytest.raises(ExecutionCancelled):
            _execute_tool(name='caller',blackboard=Blackboard(),tool_registry={},tool_name='fake',tool_config={'tool_class':Tool},arguments={},scope_context=None)
    assert not ran


def test_execution_hold_is_visible_in_portfolio_and_finalizer(task):
    from app.assistant.dayflow_orchestrator.work_context import render_view,work_data,worker_data
    store,owner=task
    with REGISTRY.span('attempt','worker',owner=owner):
        with pytest.raises(OSError):
            with tool_call('fake_action'):
                raise OSError('unknown response')
    wo=store.load(owner.work_id)
    text=render_view('portfolio',work=work_data(wo))
    assert 'EXECUTION OWNERSHIP' in text and 'unknown' in text
    text=render_view('finalizer_input',view=worker_data(wo,'main'),repeat_failure_limit=2,result_text='Timeout')
    assert 'unknown response' in text


def test_managed_executor_inherits_work_context(task):
    from app.assistant.runtime import MonitoredThreadPoolExecutor
    store,owner=task
    token=set_work_context(store,owner.work_id,'main','test')
    try:
        with REGISTRY.span('attempt','parent',owner=owner) as parent:
            with MonitoredThreadPoolExecutor(owner='test',name='ownership-test',max_workers=1) as pool:
                def child():
                    from app.assistant.manager_runtime.execution import current_span
                    return get_work_context().owner, current_span().parent.id
                child_owner,parent_id=pool.submit(child).result(timeout=5)
                assert child_owner==owner and parent_id==parent.id
    finally:
        reset_work_context(token)


def test_cross_process_snapshots_require_fresh_same_process_identity(tmp_path):
    from app.assistant.manager_runtime.execution_status import publish_snapshot,read_process_snapshots
    import json
    publish_snapshot(tmp_path,REGISTRY)
    assert len(read_process_snapshots(tmp_path,current_process_id='another-process'))==1
    path=next(tmp_path.glob('execution_*.json'))
    payload=json.loads(path.read_text(encoding='utf-8'))
    payload['process_started']=-1
    path.write_text(json.dumps(payload),encoding='utf-8')
    assert not read_process_snapshots(tmp_path,current_process_id='another-process')


def test_recovered_ticket_settles_only_matching_external_receipt(task):
    store,owner=task
    store.apply('bind_ticket',dict(work_id=owner.work_id,node_id='main',ticket_id='ticket-test',expected_dispatch_epoch=1))
    store.admit_execution_call(owner,'ticket-call','create_dayflow_ticket',True)
    store.admit_execution_call(owner,'other-call','fake_device',True)
    with store._lock,store._conn:
        store._conn.execute('UPDATE work_execution_attempts SET process_started=-1')
    store.settle_recovered_ticket(owner,'ticket-test')
    states={r['id']:r['state'] for r in store.execution_status()['calls']}
    assert states=={'ticket-call':'settled','other-call':'unknown'}


def test_cancelled_queued_future_does_not_decrement_running_worker():
    from app.assistant.runtime import MonitoredThreadPoolExecutor
    entered,release=threading.Event(),threading.Event()
    def blocking():
        entered.set()
        assert release.wait(5)
    with MonitoredThreadPoolExecutor(owner='test',name='cancel-queued',max_workers=1) as pool:
        first=pool.submit(blocking)
        try:
            assert entered.wait(5)
            with REGISTRY.span('manager','parent'):
                queued=pool.submit(lambda:None)
            assert queued.cancel()
            rows=pool._registry.snapshot()['executors']
            row=next(r for r in rows if r['executor_id']==pool._executor_id)
            assert row['running']==1
            assert not any(s['name']=='<lambda>' and s['kind']=='background' for s in REGISTRY.snapshot()['active'])
        finally:
            release.set()
        first.result(timeout=5)


@pytest.mark.parametrize('code,state',[('mcp_call_failed','unknown'),('invalid_arguments','settled')])
def test_returned_transport_failure_keeps_uncertainty(task,code,state):
    from types import SimpleNamespace
    store,owner=task
    with REGISTRY.span('attempt','worker',owner=owner):
        with tool_call('fake_mcp') as call:
            call.result=SimpleNamespace(content='failed',data={'error_code':code})
    assert store.execution_status()['calls'][0]['state']==state
