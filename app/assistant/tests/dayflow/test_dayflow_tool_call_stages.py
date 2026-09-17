"""The dayflow dispatch is a tool call like every other room's.

The switchboard names a tool; the arguments node builds that tool's arguments from
the NODE (dayflow's switchboard emits only `reason` + `delegate_to`, because the task
is the node, not prose it invents); the tool caller executes it through the shared
`execute_dispatch` and records the ToolResult on the node.

What these tests pin is the property the old dispatch lost: **nothing is conditioned
on which tool was picked.** Every call carries the same facts about its node, and a
third tool needs no change in the caller.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/dayflow/test_dayflow_tool_call_stages.py
"""
from __future__ import annotations

import pytest

from app.assistant.control_nodes.dayflow_switchboard_arguments_node import (
    DayflowSwitchboardArgumentsNode,
)
from app.assistant.control_nodes.dayflow_tool_caller import _as_tool_result
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _wo_with_node(title="Arg WO", content="ask them which evening suits"):
    store = _store()
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "n1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Confirm the evening",
                             "content": content})
    return store, wo.id, "n1"


def _run_arguments_node(delegate_to: str, ref: str):
    node = DayflowSwitchboardArgumentsNode.__new__(DayflowSwitchboardArgumentsNode)
    node.name = "dayflow_switchboard_arguments_node"
    node.blackboard = FakeBlackboard()
    node.blackboard.update_state_value("delegate_to", delegate_to)
    node.blackboard.update_state_value("work_node_ref", ref)
    node.action_handler(None)
    return node.blackboard


# ------------------------------------------------------------------ arguments


@pytest.mark.parametrize("delegate_to", ["create_dayflow_ticket", "work_emi_team_manager"])
def test_every_tool_gets_the_same_node_facts(delegate_to):
    """The payload does not depend on which tool the switchboard picked."""
    store, wid, nid = _wo_with_node()
    bb = _run_arguments_node(delegate_to, f"{wid}::{nid}")

    assert bb.get_state_value("action") == delegate_to
    args = bb.get_state_value("tool_arguments")["arguments"]
    assert args["work_id"] == wid
    assert args["node_id"] == nid
    assert args["trigger_context"] == {"work_node": f"{wid}::{nid}"}
    assert args["task"] == "Confirm the evening"
    assert args["information"] == "ask them which evening suits"


def test_the_two_tools_get_identical_payloads():
    """Same node, two targets, one payload — the only difference is the tool name."""
    store, wid, nid = _wo_with_node()
    ticket = _run_arguments_node("create_dayflow_ticket", f"{wid}::{nid}")
    worker = _run_arguments_node("work_emi_team_manager", f"{wid}::{nid}")

    assert (ticket.get_state_value("tool_arguments")["arguments"]
            == worker.get_state_value("tool_arguments")["arguments"])
    assert ticket.get_state_value("action") != worker.get_state_value("action")


def test_node_content_is_preferred_over_the_wake_primitive():
    """wake_ref is a wake-match primitive, often just the title; letting it shadow
    content is how a detailed instruction reached the user as a vague summary."""
    store, wid, nid = _wo_with_node(content="planner + pencil + instrument on Aug 21")
    args = _run_arguments_node("create_dayflow_ticket",
                               f"{wid}::{nid}").get_state_value("tool_arguments")["arguments"]
    assert args["information"] == "planner + pencil + instrument on Aug 21"


def test_links_are_absent_when_the_node_hands_over_no_research():
    store, wid, nid = _wo_with_node()
    args = _run_arguments_node("work_emi_team_manager",
                               f"{wid}::{nid}").get_state_value("tool_arguments")["arguments"]
    assert "append_links" not in args


def test_links_are_built_from_pod_ids_not_prose():
    """A pod id must reach the user exactly or not at all — never via transcription."""
    store, wid, nid = _wo_with_node()
    store.apply("attach_pod", {"work_id": wid, "node_id": nid,
                               "pod_ref": "datapod:research_finding:abc123"})
    args = _run_arguments_node("create_dayflow_ticket",
                               f"{wid}::{nid}").get_state_value("tool_arguments")["arguments"]
    assert args["append_links"] == ["Full report: /research/datapod:research_finding:abc123"]


def test_a_missing_node_ref_is_loud():
    with pytest.raises(ValueError, match="work_node_ref"):
        _run_arguments_node("work_emi_team_manager", "not-a-ref")


def test_a_missing_delegate_to_is_loud():
    store, wid, nid = _wo_with_node()
    with pytest.raises(ValueError, match="delegate_to"):
        _run_arguments_node("", f"{wid}::{nid}")


# --------------------------------------------------------------- result join


def test_a_successful_payload_becomes_a_recordable_result():
    """execute_dispatch flattens the ToolResult; the recorder reads a ToolResult's own
    contract. Without the conversion every result reads as 'the tool returned nothing'
    and the node is marked failed."""
    from work_objects.result_recorder import _answer_text, _is_failure

    result = _as_tool_result({"final_answer_answer": "they picked Thursday",
                              "final_answer_task": "Confirm the evening"})
    assert _answer_text(result) == "they picked Thursday"
    assert _is_failure(result) is False


def test_an_error_payload_stays_a_failure():
    from work_objects.result_recorder import _is_failure

    assert _is_failure(_as_tool_result({"error": True, "error_message": "boom",
                                        "final_answer_answer": "boom"})) is True


@pytest.mark.parametrize("flag", [{"aborted": True}, {"exit_state": "error_exit"}])
def test_the_tools_own_failure_flags_survive_the_flattening(flag):
    """`aborted` / `exit_state` ride in the ToolResult's data; the flattening moves them
    to the top level, and the conversion has to put them back where the recorder looks."""
    from work_objects.result_recorder import _is_failure

    payload = {"final_answer_answer": "partial", **flag}
    assert _is_failure(_as_tool_result(payload)) is True
