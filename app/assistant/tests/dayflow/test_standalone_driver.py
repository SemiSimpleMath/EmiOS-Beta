"""Standalone scenarios use real graph/result/judgment writes, fake model boundaries."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from work_objects import discharge
from work_objects.store import WorkStore
from work_objects.model import utcnow
from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
from app.assistant.utils.pydantic_classes import ToolResult

_REAL_JUDGE = WorkFinalizerNode._judge


@pytest.fixture
def runtime(monkeypatch):
    store=WorkStore(':memory:')
    calls=[]; judgments=[]
    monkeypatch.setattr(discharge,'_ensure_registered',lambda:None)
    monkeypatch.setattr(discharge.DI.manager_registry,'get',lambda name:{'node_input':'task'})
    def worker(self,**kwargs):
        from work_objects.runtime import peek_work_context
        ctx=peek_work_context()
        graph=store.load(ctx.work_id)
        node=graph.nodes[ctx.node_id]
        assert node.status=='dispatched'
        assert node.payload['dispatch_epoch']>0
        calls.append(node.id)
        return ToolResult(result_type='final_answer',content='Task answered',data={})
    monkeypatch.setattr(discharge.ManagerInterface,'invoke_on',worker)
    def judge(self,wo,node,scope):
        assert node.status=='done'
        assert any(n.type=='evidence' and n.parent_id==node.id for n in wo.nodes.values())
        judgments.append(node.id)
        return {'verdict':'achieved','outcome':'The answer meets the task.'}
    monkeypatch.setattr(WorkFinalizerNode,'_judge',judge)
    def forbidden(*args,**kwargs):
        raise AssertionError('scenario touched global Dayflow store or concern delivery')
    monkeypatch.setattr('app.assistant.dayflow_orchestrator.work_store.get_dayflow_work_store',forbidden)
    monkeypatch.setattr('app.assistant.subconscious.concern_feedback.propagate_work_outcome',forbidden)
    yield store,calls,judgments
    store.close()


def graph(store,count=2):
    wo=store.apply('create_work_object',{'title':'Scenario'})
    for i in range(count):
        store.apply('add_node',{'work_id':wo.id,'id':f'n{i}','type':'subtask',
            'title':f'Task {i}','parent_id':wo.goal_node_id})
    return wo.id


def test_dependency_chain_finishes_only_after_judgments(runtime):
    store,calls,judgments=runtime
    wid=graph(store)
    store.apply('add_edge',{'work_id':wid,'src':'n0','dst':'n1','relation':'depends_on'})
    assert discharge.drive_work(store,wid,scope_context=object())=='done'
    assert calls==judgments==['n0','n1']
    wo=store.load(wid)
    assert all(wo.nodes[n].status=='closed' for n in calls)
    assert all(wo.nodes[n].payload['finalized_epoch']==wo.nodes[n].payload['dispatch_epoch'] for n in calls)


def test_retry_does_not_run_again_or_dispatch_another_task(runtime,monkeypatch):
    store,calls,_=runtime; wid=graph(store)
    monkeypatch.setattr(WorkFinalizerNode,'_judge',lambda *a:{'verdict':'retry',
        'outcome':'Insufficient answer','recommendation':'Different approach'})
    assert discharge.drive_work(store,wid,scope_context=object())=='active'
    assert calls==['n0']
    assert discharge.drive_work(store,wid,scope_context=object())=='active'
    assert calls==['n0']


def test_recorded_result_survives_finalizer_failure_and_is_not_reexecuted(runtime,monkeypatch):
    store,calls,judgments=runtime; wid=graph(store,1)
    original=WorkFinalizerNode._judge
    def fail(*a): raise RuntimeError('model unavailable')
    monkeypatch.setattr(WorkFinalizerNode,'_judge',fail)
    with pytest.raises(RuntimeError,match='model unavailable'):
        discharge.drive_work(store,wid,scope_context=object())
    assert store.load(wid).nodes['n0'].status=='done'
    monkeypatch.setattr(WorkFinalizerNode,'_judge',original)
    assert discharge.drive_work(store,wid,scope_context=object())=='done'
    assert calls==judgments==['n0']


@pytest.mark.parametrize('gate',['time','event'])
def test_gates_do_not_dispatch(runtime,gate):
    store,calls,judgments=runtime; wid=graph(store,1)
    data={'work_id':wid,'node_id':'n0','wake_kind':gate}
    if gate=='time': data['wake_at']=(utcnow()+timedelta(hours=1)).isoformat()
    else: data['wake_ref']='external:event'
    store.apply('defer_node',data)
    assert discharge.drive_work(store,wid,scope_context=object())==('parked' if gate=='time' else 'active')
    assert not calls and not judgments


@pytest.mark.parametrize('preclaimed',[False,True])
def test_explicit_node(runtime,preclaimed):
    store,calls,judgments=runtime; wid=graph(store,1)
    if preclaimed:
        store.apply('set_status',{'work_id':wid,'node_id':'n0','status':'actionable'})
        store.apply('claim_task',{'work_id':wid,'node_id':'n0'})
    assert discharge.drive_work(store,wid,node_id='n0',scope_context=object())=='closed'
    assert calls==judgments==['n0']


def test_missing_scope_does_not_mutate(runtime):
    store,calls,_=runtime; wid=graph(store,1)
    before=store.events(wid)
    with pytest.raises(ValueError,match='scope'):
        discharge.drive_work(store,wid,scope_context=None)
    assert store.events(wid)==before and not calls


def test_real_finalizer_prompt_and_agent_boundary(runtime,monkeypatch):
    store,calls,_=runtime; wid=graph(store,1)
    monkeypatch.setattr(WorkFinalizerNode,'_judge',_REAL_JUDGE)
    seen=[]
    def create(name):
        assert name=='dayflow_orchestrator::work_finalizer'
        def action(message):
            assert 'Task answered' in message.information
            assert message.scope_context is scope
            seen.append(message)
            return SimpleNamespace(data={'verdict':'achieved','outcome':'The answer meets the task.'})
        return SimpleNamespace(action_handler=action)
    monkeypatch.setattr(discharge.DI.agent_factory,'create_agent',create)
    from work_objects.scenarios._scenario_scope import scenario_scope
    scope=scenario_scope(work_id=wid)
    assert discharge.drive_work(store,wid,scope_context=scope)=='done'
    assert len(seen)==1 and calls==['n0']


def test_smoke_scenario_checks_current_lifecycle(runtime,monkeypatch):
    store,calls,judgments=runtime
    from work_objects.scenarios import work_on_smoke
    monkeypatch.setattr(work_on_smoke,'WorkStore',lambda path:store)
    monkeypatch.setattr(work_on_smoke,'scenario_scope',lambda **kwargs:object())
    monkeypatch.setattr(work_on_smoke,'add_file_sink',lambda name:'test-log')
    monkeypatch.setenv('EMI_PRINT_PROMPTS','0')
    monkeypatch.setenv('EMI_PRINT_LLM_RESULTS','0')
    work_on_smoke.main()
    assert len(calls)==2 and calls==judgments
