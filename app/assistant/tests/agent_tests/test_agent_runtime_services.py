import pytest

from app.assistant.agent_classes.MultiToolAgent import MultiToolAgent
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.agent_runtime.services.action_validator import ActionValidator
from app.assistant.agent_runtime.services.flow_controller import FlowController
from app.assistant.agent_runtime.services.llm_client import LLMClient
from app.assistant.control_nodes.critic_pre_node import CriticPreNode
from app.assistant.control_nodes.summary_pre_node import SummaryPreNode
from app.assistant.control_nodes.dag_executor_node import DagExecutorNode
from app.assistant.control_nodes.manager_exit_node import ManagerExitNode
from app.assistant.control_nodes.tool_approve_node import ToolApproveNode
from app.assistant.multi_agent_manager_factory.MultiAgentManagerFactory import ManagerFactory
from app.assistant.manager_runtime.manager_invoker import ManagerInvoker
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message, ScopeContext
from app.assistant.utils.pipeline_state import get_pending_tool, get_resume_target


def test_action_validator_enforces_allowed_action_and_input():
    validator = ActionValidator()

    with pytest.raises(ValueError):
        validator.validate(
            agent_name="test::agent",
            result_dict={"action": "not_allowed", "action_input": "x"},
            allowed_actions={"return_control", "done", "allowed_tool"},
            list_action_validator=lambda *_args, **_kwargs: None,
        )

    with pytest.raises(ValueError):
        validator.validate(
            agent_name="test::agent",
            result_dict={"action": "allowed_tool", "action_input": ""},
            allowed_actions={"return_control", "done", "allowed_tool"},
            list_action_validator=lambda *_args, **_kwargs: None,
        )

    validator.validate(
        agent_name="test::agent",
        result_dict={"action": "return_control", "action_input": ""},
        allowed_actions={"return_control", "done", "allowed_tool"},
        list_action_validator=lambda *_args, **_kwargs: None,
    )


def test_multi_tool_list_validation_allows_allowed_nodes():
    agent = MultiToolAgent.__new__(MultiToolAgent)
    agent.name = "multi_tool_agent::planner"
    agent.get_tools = lambda: ["tool_a"]
    agent.get_allowed_nodes = lambda: ["node_a"]

    agent._validate_list_action_and_action_input(
        {"action": [["node_a"]], "action_input": ["x"], "exit": False},
        [["node_a"]],
        {"return_control", "done", "tool_a", "node_a"},
    )

    agent._validate_list_action_and_action_input(
        {"action": [["tool_a"]], "action_input": ["x"], "exit": False},
        [["tool_a"]],
        {"return_control", "done", "tool_a", "node_a"},
    )


def test_llm_client_raises_on_structured_output_failure():
    class _Blackboard:
        @staticmethod
        def get_state_value(_key):
            return None

    class _FailingInterface:
        @staticmethod
        def structured_output(*_args, **_kwargs):
            raise RuntimeError("boom")

    class _Agent:
        name = "test::agent"
        llm_params = {"llm_provider": "openai", "engine": "gpt-5-mini"}
        blackboard = _Blackboard()
        llm_interface = _FailingInterface()

        @staticmethod
        def _resolve_prompt_debug_flags():
            return False, False

        def get_llm_interface(self):
            return self.llm_interface

    client = LLMClient()
    with pytest.raises(RuntimeError, match="boom"):
        client.call_structured_output(
            agent=_Agent(),
            messages=[{"role": "user", "content": "hello"}],
            response_format=None,
            use_json=False,
        )


def test_agent_registry_callable_contract():
    registry = AgentRegistry()
    registry.configs = {
        "a": {"instance": object()},
        "b": {"instance": None},
    }
    assert set(registry.list_instantiated_agents()) == {"a"}
    assert registry.is_callable_agent("a") is True
    assert registry.is_callable_agent("b") is False

    registry.configs = {
        "x": {"instance": None},
        "y": {"instance": None},
    }
    # No instantiated entries => configured names are callable (bootstrap mode)
    assert registry.is_callable_agent("x") is True
    assert registry.is_callable_agent("z") is False


def test_dag_executor_runs_allowed_non_tool_callables_with_dependency_payload():
    class _ToolRegistry:
        @staticmethod
        def get_tool_class(_name):
            return None

        @staticmethod
        def get_tool_form(_name):
            return None

    class _CallableNode:
        def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None):
            self.name = name
            self.blackboard = blackboard
            self.agent_registry = agent_registry
            self.tool_registry = tool_registry
            self.llm_params = llm_params or {}

        def action_handler(self, message):
            return {"ok": True, "content": "node-ok", "received": message.agent_input}

    class _AgentRegistry:
        @staticmethod
        def get_agent_config(name):
            if name == "node_a":
                return {"type": "agent", "llm_params": {}}
            return None

        @staticmethod
        def get_agent_class(name):
            if name == "node_a":
                return _CallableNode
            return None

    node = DagExecutorNode(
        name="dag_executor_node",
        blackboard=Blackboard(),
        agent_registry=_AgentRegistry(),
        tool_registry=_ToolRegistry(),
    )
    res = node.execute_sequence_sync(
        sequence_id="seq1",
        steps=[
            {"id": "seq1::step1", "tool_name": "node_a", "action_input": "hello"},
            {"id": "seq1::step2", "tool_name": "node_a", "depends_on": ["seq1::step1"]},
        ],
        initial_input="",
        task_text="test",
    )

    assert res.get("ok") is True
    step2 = res.get("steps", {}).get("seq1::step2", {})
    received = step2.get("data", {}).get("received", {})
    assert received.get("previous_step_id") == "seq1::step1"
    assert isinstance(received.get("previous_result"), dict)


def test_manager_exit_materializes_final_answer_from_state_fields():
    bb = Blackboard()
    bb.update_state_value("final_answer_answer", "Hello")
    bb.update_state_value("final_answer_sources", [])
    bb.update_state_value("final_answer_detail_level", "brief")
    bb.update_state_value("final_answer_data_list", [])
    node = ManagerExitNode(
        name="manager_exit_node",
        blackboard=bb,
        agent_registry=AgentRegistry(),
        tool_registry=object(),
    )
    node.action_handler(message=None)
    final_answer = bb.get_state_value("final_answer")
    assert isinstance(final_answer, dict)
    assert final_answer.get("final_answer_answer") == "Hello"


def test_manager_exit_preserves_result_answer_when_only_pods_set():
    """Regression (audit): pod_references/result_summary must NOT flip has_state_final, else a
    pod-bearing dispatch with no final_answer_* set would drop the dispatched manager's `result`
    answer text (the dayflow path)."""
    bb = Blackboard()
    bb.update_state_value("result", {"final_answer_answer": "The dispatched answer.", "final_answer_sources": []})
    bb.update_state_value("accumulated_pod_references", [{"pod_id": "datapod:research_finding:aaaaaa", "one_liner": "A"}])
    bb.update_state_value("result_summary", "did the thing")
    node = ManagerExitNode(name="manager_exit_node", blackboard=bb, agent_registry=AgentRegistry(), tool_registry=object())
    node.action_handler(message=None)
    fa = bb.get_state_value("final_answer")
    assert fa.get("final_answer_answer") == "The dispatched answer."  # not dropped
    assert [r["pod_id"] for r in fa.get("pod_references")] == ["datapod:research_finding:aaaaaa"]
    assert fa.get("result_summary") == "did the thing"


def test_manager_exit_unions_relayed_pods_over_empty_form():
    """Regression (audit): a final-answer agent emitting an EMPTY pod_references form field must not
    clobber the accumulated relay — the harvest unions the relay key with the form key."""
    bb = Blackboard()
    bb.update_state_value("final_answer_answer", "Web summary.")
    bb.update_state_value("final_answer_sources", [])
    bb.update_state_value("final_answer_detail_level", "brief")
    bb.update_state_value("final_answer_data_list", [])
    bb.update_state_value("pod_references", [])  # final_answer_lite emitted its default-empty field
    bb.update_state_value("accumulated_pod_references", [{"pod_id": "datapod:research_finding:bbbbbb", "one_liner": "B"}])
    node = ManagerExitNode(name="manager_exit_node", blackboard=bb, agent_registry=AgentRegistry(), tool_registry=object())
    node.action_handler(message=None)
    fa = bb.get_state_value("final_answer")
    assert [r["pod_id"] for r in fa.get("pod_references")] == ["datapod:research_finding:bbbbbb"]  # relay survived


def test_flow_controller_nested_return_control_defers_routing_to_handler():
    class _Blackboard:
        def __init__(self):
            self.state = {"next_agent": "caller::agent", "last_agent": "caller::agent"}

        def get_current_call_context(self):
            return ("caller::agent", "callee::agent", "scope_1")

        def update_state_value(self, key, value):
            self.state[key] = value

    class _Agent:
        name = "callee::agent"

        def __init__(self):
            self.blackboard = _Blackboard()

    agent = _Agent()
    FlowController().route(agent, {"action": "return_control", "result": {"ok": True}})

    assert agent.blackboard.state.get("result") == {"ok": True}
    # Routing is left untouched in nested scope; ToolResultHandler decides.
    assert agent.blackboard.state.get("next_agent") == "caller::agent"
    assert agent.blackboard.state.get("last_agent") == "caller::agent"


def test_flow_controller_top_level_return_control_sets_exit_signal():
    class _Blackboard:
        def __init__(self):
            self.state = {}

        def get_current_call_context(self):
            return ("manager", "manager", "root_scope")

        def update_state_value(self, key, value):
            self.state[key] = value

    class _Agent:
        name = "planner::agent"

        def __init__(self):
            self.blackboard = _Blackboard()

    agent = _Agent()
    FlowController().route(agent, {"action": "return_control", "result": {"ok": True}})
    assert agent.blackboard.state.get("next_agent") is None
    assert agent.blackboard.state.get("last_agent") == "planner::agent_return_control"


def test_tool_approve_node_adds_installed_tool_to_runtime_scope(monkeypatch):
    class _InstalledRecord:
        def __init__(self, namespaced_tool_name: str):
            self.namespaced_tool_name = namespaced_tool_name

    class _Blackboard:
        def __init__(self):
            self.state = {
                "pending_tool_approval": {
                    "tool_name": "mcp::npm/server-google-maps::maps_distance_matrix",
                    "calling_agent": "emi_team::planner",
                },
                "dynamic_allowed_tools": ["find_tool", "install_tool"],
                "recently_installed_tools": [],
                "visible_tools": ["find_tool", "install_tool"],
                "tool_visibility_max": 12,
            }

        def get_state_value(self, key):
            return self.state.get(key)

        def update_state_value(self, key, value):
            self.state[key] = value

    monkeypatch.setattr(
        "app.assistant.control_nodes.tool_approve_node.list_installed_records",
        lambda enabled_only=True: [_InstalledRecord("mcp::npm/server-google-maps::maps_distance_matrix")],
    )

    bb = _Blackboard()
    node = ToolApproveNode(
        name="tool_approve_node",
        blackboard=bb,
        agent_registry=AgentRegistry(),
        tool_registry=object(),
    )
    node.action_handler(message=None)

    assert bb.get_state_value("next_agent") == "emi_team::planner"
    assert "mcp::npm/server-google-maps::maps_distance_matrix" in (bb.get_state_value("dynamic_allowed_tools") or [])
    assert "mcp::npm/server-google-maps::maps_distance_matrix" in (bb.get_state_value("visible_tools") or [])
    assert bb.get_state_value("pending_tool_approval") is None


def test_agent_get_tools_supports_list_all_sentinel():
    class _ToolRegistry:
        @staticmethod
        def list_tools():
            return ["find_tool", "install_tool", "ask_user"]

    class _AgentRegistry:
        @staticmethod
        def get_agent_config(_name):
            return {"allowed_tools": ["all"], "except_tools": ["ask_user"]}

    from app.assistant.agent_classes.Agent import Agent

    a = Agent.__new__(Agent)
    a.name = "test::agent"
    a.agent_registry = _AgentRegistry()
    a.tool_registry = _ToolRegistry()
    a.blackboard = None

    tools = set(a.get_tools())
    assert "find_tool" in tools
    assert "install_tool" in tools
    assert "ask_user" not in tools


def test_manager_factory_supports_list_all_sentinel_for_manager_tools(monkeypatch):
    class _Registry:
        @staticmethod
        def get(_manager_type):
            return {
                "class_name": "MultiAgentManager",
                "tools": {"allowed_tools": ["all"], "except_tools": ["ask_user"]},
            }

        @staticmethod
        def register_instance(_manager_type, _instance):
            return None

    class _ToolRegistry:
        def __init__(self):
            self.filtered_arg = None

        @staticmethod
        def list_tools():
            return ["find_tool", "install_tool", "ask_user"]

        def filter_tools(self, allowed):
            self.filtered_arg = set(allowed)
            return self

    class _DummyManager:
        def __init__(self, name, config, tool_registry, agent_registry):
            self.name = name
            self.config = config
            self.tool_registry = tool_registry
            self.agent_registry = agent_registry

    tool_registry = _ToolRegistry()
    factory = ManagerFactory(registry=_Registry(), tool_registry=tool_registry, agent_registry=object())
    monkeypatch.setattr(factory, "_import_class", lambda _class_name: _DummyManager)

    _ = factory.create("emi_team_manager")
    assert tool_registry.filtered_arg == {"find_tool", "install_tool"}


def test_context_injector_uses_custom_agent_history_builder():
    from app.assistant.agent_runtime.services.context_injector import ContextInjector

    class _Blackboard:
        @staticmethod
        def get_state_value(_key, default=None):
            return default

    class _Agent:
        name = "emi_agent"
        blackboard = _Blackboard()

        @staticmethod
        def build_history():
            return "CUSTOM CHAT HISTORY"

    injector = ContextInjector()
    ctx = injector.generate_injections_block(_Agent(), ["history"])
    assert ctx.get("history") == "CUSTOM CHAT HISTORY"


def test_agent_default_build_history_is_empty():
    from app.assistant.agent_classes.Agent import Agent

    agent = Agent.__new__(Agent)
    assert agent.build_history() == ""




def test_context_injector_prefers_injected_history_over_builder():
    from app.assistant.agent_runtime.services.context_injector import ContextInjector

    class _Blackboard:
        @staticmethod
        def get_state_value(key, default=None):
            if key == "history":
                return "INJECTED HISTORY SNAPSHOT"
            return default

    class _Agent:
        name = "emi_agent"
        blackboard = _Blackboard()

        @staticmethod
        def build_history():
            return "BUILDER HISTORY"

    ctx = ContextInjector().generate_injections_block(_Agent(), ["history"])
    assert ctx.get("history") == "INJECTED HISTORY SNAPSHOT"





def test_critic_pre_node_intercepts_planner_to_critic_and_builds_payload():
    from app.assistant.utils.pipeline_state import set_pending_tool

    bb = Blackboard()
    bb.update_state_value("last_agent", "playwright::planner")
    bb.update_state_value("playwright::planner_action_count", 5)
    bb.update_state_value(
        "manager_flow_config",
        {
            "critic": {
                "enabled": True,
                "subject_agent": "playwright::planner",
                "critic_agent": "playwright::critic",
                "continue_agent": "shared::tool_arguments",
                "cadence_every_actions": 5,
                "action_count_state_key": "{planner_agent}_action_count",
                "run_on_last_tool_error": False,
                "must_revise_flag_key": "must_revise_plan",
                "payload_key": "critic_payload",
                "payload_recent_history_messages": 20,
                "next_if_no_pending": "playwright::planner",
                "trigger_reason_key": "web_critic_trigger_reason",
            }
        },
    )
    set_pending_tool(
        bb,
        name="mcp::npm/playwright-mcp::browser_click",
        calling_agent="playwright::planner",
        action_input={"element": "submit"},
        arguments=None,
        kind="tool",
    )

    node = CriticPreNode(
        name="critic_pre_node",
        blackboard=bb,
        agent_registry=object(),
        tool_registry=object(),
    )
    node.action_handler(Message(data_type="agent_activation", data={}))

    assert bb.get_state_value("next_agent") == "playwright::critic"
    assert get_resume_target(bb) == "shared::tool_arguments"
    assert get_pending_tool(bb)["name"] == "mcp::npm/playwright-mcp::browser_click"
    payload = bb.get_state_value("critic_payload")
    assert isinstance(payload, dict)
    assert payload.get("subject_agent") == "playwright::planner"
    assert payload.get("subject_action") == "mcp::npm/playwright-mcp::browser_click"


def test_summary_pre_node_routes_to_summary_agent_and_builds_payload():
    bb = Blackboard()
    bb.update_state_value("last_agent", "tool_result_handler")
    bb.update_state_value("web::planner_action_count", 4)
    bb.update_state_value(
        "manager_flow_config",
        {
            "summary": {
                "enabled": True,
                "source_agent": "tool_result_handler",
                "summary_agent": "shared::web_summary",
                "resume_agent": "web::planner",
                "planner_agent": "web::planner",
                "cadence_every_actions": 4,
                "action_count_state_key": "{planner_agent}_action_count",
                "min_messages": 1,
                "trigger_reason_key": "web_summary_trigger_reason",
                "payload_key": "summary_payload",
                "payload_recent_history_messages": 20,
            }
        },
    )
    bb.add_msg(Message(data_type="agent_result", sender="web::planner", content="step one"))

    node = SummaryPreNode(
        name="summary_pre_node",
        blackboard=bb,
        agent_registry=object(),
        tool_registry=object(),
    )
    node.action_handler(Message(data_type="agent_activation", data={}))

    assert bb.get_state_value("next_agent") == "shared::web_summary"
    payload = bb.get_state_value("summary_payload")
    assert isinstance(payload, dict)
    assert payload.get("trigger_reason") == "cadence"


def test_scope_adapter_strict_mode_rejects_missing_scope(monkeypatch):
    from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter

    monkeypatch.setenv("SCOPE_CONTRACT_STRICT", "true")
    adapter = ScopeAdapter()
    msg = Message(data_type="user_message", sender="user", task="hello", information="")
    with pytest.raises(ValueError, match="Missing scope_context"):
        adapter.apply(manager_name="test_manager", manager_config={}, message=msg)


def test_manager_invoker_with_scope_projected_data_sets_scope_enforcement():
    class _Manager:
        name = "test_manager"
        manager_config = {}

        @staticmethod
        def request_handler(message):
            return message

    scope = ScopeContext(
        scope_id="scope::test",
        owner_id="owner",
        actor_id="actor",
        surface="ui",
    )
    invoker = ManagerInvoker()
    msg = Message(
        data_type="user_message",
        sender="user",
        task="hello",
        information="",
        scope_context=scope,
    )
    out = invoker.invoke(_Manager(), msg)
    assert isinstance(out, Message)
    assert isinstance(out.scope_context, ScopeContext)
    assert out.data["scope_contract_enforced"] is True


def test_agent_enforced_scope_requires_effective_scope():
    from app.assistant.agent_classes.Agent import Agent

    class _Blackboard:
        def __init__(self):
            self.state = {"scope_contract_enforced": True}

        def update_state_value(self, key, value):
            self.state[key] = value

        def get_state_value(self, key, default=None):
            return self.state.get(key, default)

    agent = Agent.__new__(Agent)
    agent.name = "test::agent"
    agent.blackboard = _Blackboard()

    with pytest.raises(ValueError, match="Missing scope_context"):
        agent._update_blackboard_state(Message(data_type="agent_activation"))

