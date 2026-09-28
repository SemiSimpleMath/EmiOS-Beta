from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from work_objects.store import WorkStore
from work_objects.runtime import set_work_context, reset_work_context
from work_objects.tools import WorkGraphTools
from work_objects.work_tools import register_work_tools
from app.assistant.utils.pydantic_classes import ToolMessage, ScopeContext, ScopeToolPolicy, ScopePodPolicy, ScopeApprovalPolicy
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.workobject_render_node import render_work_projection

BODY = 'Complete fridge research: Glacier 220 costs $529; Meadow 240 costs $649; Summit 260 costs $799. Recommend Glacier 220. ' + 'Detailed measured research. ' * 100
POD_ID = 'datapod:research_finding:fridge-evaluation'

def make_work(store):
    wo=store.apply('create_work_object', {'title':'Household research', 'goal_content':'Compare household options and share the selected research.'})
    for nid,title,content in [('research-a','Fridge research','Compare three fridges.'),('research-b','Oven research','Compare ovens.'),('send-task','Email research','Email the fridge research to the user.')]:
        store.apply('add_node',dict(work_id=wo.id,id=nid,type='subtask',parent_id=wo.goal_node_id,title=title,content=content))
    store.apply('add_node',dict(work_id=wo.id,id='artifact-a',type='artifact',parent_id='research-a',title='Fridge comparison report',pod_ref=POD_ID))
    store.apply('add_node',dict(work_id=wo.id,id='evidence-a',type='evidence',parent_id='research-a',title='Fridge dimensions',content=BODY))
    store.apply('set_status',dict(work_id=wo.id,node_id='research-a',status='abandoned',reason='Research retained; this attempt ended.'))
    store.apply('set_status',dict(work_id=wo.id,node_id='send-task',status='actionable'))
    store.apply('claim_task',dict(work_id=wo.id,node_id='send-task'))
    return wo.id

@pytest.fixture
def case(tmp_path):
    store=WorkStore(str(tmp_path/'work.db')); wid=make_work(store)
    store.other_id=store.apply('create_work_object',dict(title='Other',goal_content='Other')).goal_node_id
    token=set_work_context(store,wid,'send-task','evaluation')
    register_work_tools(DI.tool_registry)
    yield store,wid
    reset_work_context(token);store.close()

def scope():
    return ScopeContext(scope_id='eval',owner_id='user',actor_id='eval',surface='work_objects',
        tools=ScopeToolPolicy(allowed_tools=['all']),pods=ScopePodPolicy(allowed_scopes=['all']),approval=ScopeApprovalPolicy(authority_level=99))

def call(name,args):
    return DI.tool_registry.registry[name]['tool_class']().execute(ToolMessage(tool_name=name,tool_data={'tool_name':name,'arguments':args},scope_context=scope()))

def test_progressive_view(case):
    store,wid=case
    text=render_work_projection(store.load(wid),'send-task')
    assert 'Fridge research' in text and 'Oven research' in text
    assert 'Email the fridge research to the user.' in text
    assert BODY not in text and 'Fridge comparison report' not in text and POD_ID not in text
    result=call('work_graph_peek',{'node_id':'research-a'})
    assert 'Fridge comparison report' in result.content and 'artifact-a' in result.content
    assert BODY not in result.content
    assert call('work_artifact_fetch',{'artifact_id':'evidence-a'}).data['artifact']['content']==BODY
    assert BODY in call('work_artifact_fetch',{'artifact_id':'evidence-a'}).content

def test_pod_hydration_uses_full_fetch(case,monkeypatch):
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import PodStoreTool
    from app.assistant.pod_store.contracts import Pod
    pod=Pod(pod_id=POD_ID,kind='research_finding',one_liner='Fridge report',body=BODY,scope_id='eval')
    monkeypatch.setattr(PodStoreTool,'_ensure_store',lambda self: SimpleNamespace(get=lambda pid: pod if pid==POD_ID else None))
    result=call('work_artifact_fetch',{'artifact_id':'artifact-a'})
    assert BODY in result.content

def test_reads_do_not_cross_work_objects(case):
    store,wid=case
    with pytest.raises(KeyError): WorkGraphTools(store,wid,'send-task','eval').graph_peek(store.other_id)

def test_read_capability_preserves_explicit_scope_denials(case):
    from app.assistant.lib.tool_execution.tool_access_control import check_tool_access
    blocked=scope();blocked.tools.allowed_tools=['send_email']
    ok,_=check_tool_access(tool_name='work_graph_peek',scope_contract_enforced=True,scope_context=blocked,task_allowed_tools=None,task_except_tools=None,caller_name='test')
    assert not ok
    blocked.tools.allowed_tools=['all'];blocked.tools.blocked_tools=['work_graph_peek']
    ok,_=check_tool_access(tool_name='work_graph_peek',scope_contract_enforced=True,scope_context=blocked,task_allowed_tools=None,task_except_tools=None,caller_name='test')
    assert not ok


def test_nested_worker_sees_owning_task_history(case):
    store,wid=case
    store.apply('add_node',dict(work_id=wid,id='parent-result',type='evidence',parent_id='send-task',title='Research prepared',content=BODY,pod_ref=POD_ID))
    store.apply('add_node',dict(work_id=wid,id='admin-helper',type='subtask',parent_id='send-task',title='Email research',content='Email the fridge research to the user.'))
    wo=store.load(wid)
    view=render_work_projection(wo,'admin-helper')
    assert 'OWNING MAIN TASK' in view
    assert BODY in view and POD_ID in view
    assert not wo.is_work_unit(wo.nodes['admin-helper'])
    assert 'Fridge comparison report' not in view  # unrelated task stays compact


def test_work_personal_admin_standard_wiring():
    from pathlib import Path
    import yaml
    from app.assistant.agents.work_personal_admin.planner.agent_form import AgentForm
    root=Path(__file__).resolve().parents[2]
    config=yaml.safe_load((root/'multi_agents/work_personal_admin_manager/config.yaml').read_text(encoding='utf-8'))
    assert config['node_aware'] and config['node_input']=='render'
    assert {'name':'work_personal_admin::planner','class':'WorkPlanner'} in config['agents']
    assert config['flow_config']['state_map']['personal_admin::delegator']=='workobject_render_node'
    assert 'findings' in AgentForm.model_fields
    scope=yaml.safe_load((root/'rooms/dayflow_orchestrator/scope.yaml').read_text(encoding='utf-8'))
    assert 'work_personal_admin_manager' in scope['tools']['per_manager']['work_emi_team_manager']['allow']


def test_node_handoff_preserves_information(case,monkeypatch):
    from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface
    from app.assistant.utils.pydantic_classes import ToolResult
    store,wid=case
    manager=ManagerInterface('work_personal_admin_manager')
    observed=[]
    def invoke(work_id,node_id,message):
        observed.append(store.load(work_id).nodes[node_id])
        return ToolResult(content='Dry test: no external action.')
    monkeypatch.setattr(manager,'_run_on_given_node',invoke)
    instruction='Email the research. ' + 'Keep this complete title. '*10
    manager._run_on_child_node(instruction,BODY,ToolMessage(tool_name='work_personal_admin_manager',tool_data={'arguments':{}},scope_context=scope()))
    assert len(observed)==1
    assert observed[0].title==instruction.strip()
    assert BODY in observed[0].content
    assert not store.load(wid).is_work_unit(observed[0])
