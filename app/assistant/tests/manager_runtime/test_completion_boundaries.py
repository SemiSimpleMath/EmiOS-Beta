"""Manager termination must retain failure identity and completed boundary results."""
from types import SimpleNamespace
from unittest.mock import Mock
from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from work_objects.result_recorder import _is_failure


def manager(state=None):
    m = MultiAgentManager.__new__(MultiAgentManager)
    m.name = "isolated"
    m.blackboard = FakeBlackboard(state or {})
    m.blackboard.update_global_state_value = m.blackboard.update_state_value
    m._drain_mailbox = Mock()
    m._emit_route_trace = Mock()
    m.resolve_role_binding = lambda name: name
    return m


def test_default_error_exit_is_structured_failure():
    result = manager({"error_message": "synthetic failure"}).handle_default_error_exit()
    assert result.result_type == "manager_aborted"
    assert _is_failure(result)


def test_last_allowed_agent_can_complete():
    m = manager()
    m.agent_registry = {"agent": SimpleNamespace(action_handler=lambda msg: m.blackboard.update_state_value("exit", True))}
    m.agent_registry = SimpleNamespace(get_agent_instance=m.agent_registry.get)
    delegator = SimpleNamespace(action_handler=lambda msg: m.blackboard.update_state_value("next_agent", "agent"))
    assert m._run_loop(1, delegator, {}) == "success"


def test_last_allowed_agent_can_reach_control_node_finalizer():
    m = manager()
    finalizer = ControlNode.__new__(ControlNode)
    finalizer.action_handler = lambda msg: m.blackboard.update_state_value("exit", True)
    m.agent_registry = {"agent": SimpleNamespace(action_handler=lambda msg: m.blackboard.update_state_value("last_agent", "agent")), "finalizer": finalizer}
    m.agent_registry = SimpleNamespace(get_agent_instance=m.agent_registry.get)
    def route(msg):
        m.blackboard.update_state_value("next_agent", "finalizer" if m.blackboard.get_state_value("last_agent") else "agent")
    assert m._run_loop(1, SimpleNamespace(action_handler=route), {}) == "success"
