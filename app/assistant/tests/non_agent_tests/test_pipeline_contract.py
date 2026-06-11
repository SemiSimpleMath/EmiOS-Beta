from app.assistant.control_nodes.tool_caller import ToolCaller
from app.assistant.control_nodes.critic_post_node import CriticPostNode
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message, ToolResult
from app.assistant.utils.pipeline_state import set_resume_target
from app.assistant.utils.history_formatting import format_recent_history


class _ToolRegistryStub:
    def __init__(self, tool_class):
        self._tool_class = tool_class

    def get_tool(self, name):
        return {"tool_class": self._tool_class}

    def get_mcp_server_entry(self, _server_id):
        return None


class _AgentRegistryStub:
    def __init__(self, tool_result_handler=None, delegator_config=None):
        self._tool_result_handler = tool_result_handler
        self._delegator_config = delegator_config or {}

    def get_agent_config(self, name: str):
        if name == "playwright::delegator":
            return self._delegator_config
        return {"color": "green", "prompts": {"system": "", "user": ""}}

    def get_agent_instance(self, name: str):
        if name == "tool_result_handler":
            return self._tool_result_handler
        return None

    def get_agent_input_form(self, _agent_name: str):
        return None


class _ToolResultHandlerStub:
    def __init__(self, bb: Blackboard):
        self.bb = bb
        self.seen_tool_request = False
        self.last_tool_result = None

    def process_tool_result_direct(self, tool_result=None):
        self.last_tool_result = tool_result
        self.seen_tool_request = any(getattr(m, "data_type", None) == "tool_request" for m in self.bb.get_messages())


class _SimpleTool:
    def execute(self, _tool_message):
        return ToolResult(result_type="tool_success", content="ok", data={"value": "ok"})


def test_pipeline_order_tool_request_before_result():
    """ToolCaller logs the tool_request message BEFORE the result handler
    runs. The planner's selection arrives via blackboard state: `action`
    (target name), `tool_arguments` ({target_name, arguments}), and
    `calling_agent`."""
    bb = Blackboard()
    handler = _ToolResultHandlerStub(bb)
    tool_registry = _ToolRegistryStub(_SimpleTool)
    agent_registry = _AgentRegistryStub(tool_result_handler=handler)
    bb.update_state_value("action", "simple_tool")
    bb.update_state_value("tool_arguments", {"target_name": "simple_tool", "arguments": {}})
    bb.update_state_value("calling_agent", "test_agent")

    tool_caller = ToolCaller("tool_caller", bb, agent_registry, tool_registry)
    tool_caller.action_handler(Message(data_type="agent_activation"))

    assert bb.get_state_value("error") is not True, bb.get_state_value("error_message")
    assert handler.seen_tool_request is True
    assert isinstance(handler.last_tool_result, ToolResult)


def test_critic_post_routes_to_resume_target():
    """After a no-revise critic verdict, critic_post routes to the saved
    resume target and clears the per-trigger critic state. The subject
    agent comes from `critic_subject_agent` state (set by critic_pre when
    it triggers the critic), not from config."""
    bb = Blackboard()
    bb.update_state_value(
        "manager_flow_config",
        {
            "critic": {
                "enabled": True,
                "subject_agent": "playwright::planner",
                "critic_agent": "playwright::critic",
                "continue_agent": "shared::tool_arguments",
                "must_revise_flag_key": "must_revise_plan",
                "cadence_every_actions": 5,
                "action_count_state_key": "{planner_agent}_action_count",
                "run_on_last_tool_error": False,
            }
        },
    )
    agent_registry = _AgentRegistryStub()
    tool_registry = _ToolRegistryStub(_SimpleTool)
    node = CriticPostNode("critic_post_node", bb, agent_registry, tool_registry)

    bb.update_state_value("critic_subject_agent", "playwright::planner")
    set_resume_target(bb, "tool_caller")
    bb.update_state_value("must_revise_plan", False)
    bb.update_state_value("last_agent", "playwright::critic")

    node.action_handler(Message(data_type="agent_activation", data={}))

    assert bb.get_state_value("next_agent") == "tool_caller"
    assert bb.get_state_value("critic_subject_agent") is None


def test_critic_post_revise_routes_back_to_subject():
    """A must-revise verdict routes back to the subject agent instead of
    the resume target, and clears the revise flag."""
    bb = Blackboard()
    bb.update_state_value(
        "manager_flow_config",
        {
            "critic": {
                "enabled": True,
                "critic_agent": "playwright::critic",
                "continue_agent": "shared::tool_arguments",
                "must_revise_flag_key": "must_revise_plan",
            }
        },
    )
    node = CriticPostNode("critic_post_node", bb, _AgentRegistryStub(), _ToolRegistryStub(_SimpleTool))

    bb.update_state_value("critic_subject_agent", "playwright::planner")
    set_resume_target(bb, "tool_caller")
    bb.update_state_value("must_revise_plan", True)
    bb.update_state_value("last_agent", "playwright::critic")

    node.action_handler(Message(data_type="agent_activation", data={}))

    assert bb.get_state_value("next_agent") == "playwright::planner"
    assert bb.get_state_value("must_revise_plan") is False


def test_history_ordering_stub_for_summarized_results():
    """A summarized tool result renders its summary and suppresses the full
    content; un-summarized results render in full."""
    msgs = [
        Message(data_type="tool_request", sender="planner", receiver="tool", content="Calling tool X"),
        Message(data_type="tool_result", sender="tool", receiver="planner", content="full result", metadata={"tool_result_id": "t1"}),
        Message(data_type="tool_result_summary", sender="planner", receiver="Blackboard", content="summary 1", metadata={"summarizes_tool_result_id": "t1"}),
        Message(data_type="tool_request", sender="planner", receiver="tool", content="Calling tool Y"),
        Message(data_type="tool_result", sender="tool", receiver="planner", content="full result 2", metadata={"tool_result_id": "t2"}),
    ]
    out = format_recent_history(msgs)
    assert "TOOL CALL" in out
    assert "Summary: summary 1" in out          # t1 renders the summary...
    assert "Result: full result\n" not in out   # ...with its full content suppressed
    assert "Result: full result 2" in out       # t2 (no summary) renders in full
