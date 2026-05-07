from app.assistant.control_nodes.summary_context_node import SummaryContextNode
from app.assistant.control_nodes.maybe_summary_gate import MaybeSummaryGate
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pipeline_state import set_resume_target
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.history_formatting import format_recent_history


class _DummyAgentRegistry:
    pass


class _DummyToolRegistry:
    pass


def test_summary_context_node_compacts_and_resumes():
    bb = Blackboard()
    bb.update_state_value("summary_context_threshold_messages", 6)
    bb.update_state_value("summary_context_keep_recent_messages", 2)
    bb.update_state_value("summary_context_max_summary_lines", 4)
    bb.update_state_value("summary_context_max_summary_chars", 500)
    bb.update_state_value("summary_context_pin_keywords", ["critical"])
    set_resume_target(bb, "shared::tool_arguments")

    for i in range(8):
        txt = f"step {i} regular progress"
        if i == 2:
            txt = "critical constraint from earlier step"
        bb.add_msg(
            Message(
                data_type="agent_result",
                sender="playwright::planner",
                content=txt,
            )
        )

    node = SummaryContextNode(
        name="summary_context_node",
        blackboard=bb,
        agent_registry=_DummyAgentRegistry(),
        tool_registry=_DummyToolRegistry(),
    )
    node.action_handler(message=None)

    msgs = bb.get_messages()
    suppressed = [
        m for m in msgs
        if isinstance(getattr(m, "metadata", None), dict) and bool(m.metadata.get("context_suppressed", False))
    ]
    pinned = [
        m for m in msgs
        if isinstance(getattr(m, "metadata", None), dict) and bool(m.metadata.get("context_pinned", False))
    ]
    summaries = [
        m for m in msgs
        if "context_summary" in (getattr(m, "sub_data_type", []) or [])
    ]

    assert len(suppressed) > 0
    assert len(pinned) > 0
    assert len(summaries) == 1
    assert bb.get_state_value("next_agent") == "shared::tool_arguments"


def test_history_formatting_skips_suppressed_unless_pinned():
    visible = Message(
        data_type="tool_result",
        sender="tool_caller",
        content="visible tool result",
        metadata={"tool_result_id": "r1"},
        sub_data_type=["demo_tool"],
    )
    hidden = Message(
        data_type="tool_result",
        sender="tool_caller",
        content="should be hidden",
        metadata={"context_suppressed": True, "tool_result_id": "r2"},
        sub_data_type=["demo_tool"],
    )
    pinned = Message(
        data_type="tool_result",
        sender="tool_caller",
        content="should stay visible",
        metadata={"context_suppressed": True, "context_pinned": True, "tool_result_id": "r3"},
        sub_data_type=["demo_tool"],
    )

    rendered = format_recent_history([visible, hidden, pinned])
    assert "visible tool result" in rendered
    assert "should stay visible" in rendered
    assert "should be hidden" not in rendered


def test_maybe_summary_gate_condition_true_when_long_context():
    bb = Blackboard()
    bb.update_state_value("playwright::planner_action_count", 8)
    bb.update_state_value("last_agent", "playwright::planner")
    bb.update_state_value(
        "manager_flow_config",
        {
            "gates": {
                "summary": {
                    "enabled": True,
                    "planner_agent": "playwright::planner",
                    "tool_arguments_agent": "shared::tool_arguments",
                    "summary_node": "summary_context_node",
                    "summary_resume_target": "shared::tool_arguments",
                    "next_if_skipped": "shared::tool_arguments",
                    "summary_min_messages": 140,
                    "summary_cadence_every_actions": 4,
                    "summary_last_action_count_key": "playwright_summary_last_action_count",
                }
            }
        },
    )
    from app.assistant.utils.pipeline_state import set_pending_tool
    set_pending_tool(
        bb,
        name="search_web",
        calling_agent="playwright::planner",
        action_input={"query": "x"},
        arguments=None,
        kind="tool",
    )
    for i in range(145):
        bb.add_msg(Message(data_type="agent_result", sender="s", content=f"m{i}"))

    node = MaybeSummaryGate(
        name="maybe_summary_gate",
        blackboard=bb,
        agent_registry=_DummyAgentRegistry(),
        tool_registry=_DummyToolRegistry(),
    )
    node.action_handler(message=None)
    assert bb.get_state_value("next_agent") == "summary_context_node"
