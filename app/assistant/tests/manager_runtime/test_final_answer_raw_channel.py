"""The typed result channel: data.final_answer_raw on the manager ToolResult.

The normalized final-answer envelope stringifies structure (leftover keys
become data_list detail strings), so callers needing an agent's raw
STRUCTURED output used to scrape the finished manager's blackboard audit
messages. Now: the exit nodes stash the pre-normalization payload under
``final_answer_raw`` (manager_exit_node additionally captures the terminal
agent's output when no result/final_answer_* was routed), and handle_exit
attaches it to ToolResult.data on success exits.
"""
from __future__ import annotations

from app.assistant.control_nodes.manager_exit_node import ManagerExitNode
from app.assistant.agent_runtime.services.final_answer_normalizer import FinalAnswerNormalizer
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager
from app.assistant.utils.pydantic_classes import Message, ToolResult

PLAN = {"week_start_date": "2026-07-13", "days": [{"day": "Mon", "dinner": "tacos"}]}


def _exit_node(blackboard: Blackboard) -> ManagerExitNode:
    return ManagerExitNode("manager_exit_node", blackboard, None, None)


def _bare_manager(blackboard: Blackboard) -> MultiAgentManager:
    mgr = MultiAgentManager.__new__(MultiAgentManager)
    mgr.name = "test_manager"
    mgr.blackboard = blackboard
    mgr.flow_config = {}
    mgr.manager_config = {}
    return mgr


class TestManagerExitNodeRawCapture:

    def test_terminal_agent_output_becomes_final_answer_raw(self):
        bb = Blackboard()
        # A form-driven terminal agent wrote its audit message; nothing set
        # `result` or final_answer_* (the weekly_meal_planner shape).
        bb.add_msg(Message(data_type="agent_result", sender="weekly_meal_planner", data=PLAN))

        _exit_node(bb).action_handler(Message())

        assert bb.get_state_value("final_answer_raw") == PLAN
        assert isinstance(bb.get_state_value("final_answer"), dict)
        assert bb.get_state_value("exit") is True

    def test_explicit_result_wins_over_terminal_capture(self):
        bb = Blackboard()
        bb.add_msg(Message(data_type="agent_result", sender="some_agent", data={"noise": True}))
        bb.update_state_value("result", PLAN)

        _exit_node(bb).action_handler(Message())

        assert bb.get_state_value("final_answer_raw") == PLAN


class TestHandleExitAttachesRaw:

    def test_success_exit_carries_final_answer_raw(self):
        bb = Blackboard()
        bb.update_state_value("result", PLAN)
        _exit_node(bb).action_handler(Message())

        result = _bare_manager(bb).handle_exit()
        assert isinstance(result, ToolResult)
        assert result.result_type == "final_answer"
        assert result.data["final_answer_raw"] == PLAN
        # The blackboard's normalized dict must not be mutated by the attach.
        assert "final_answer_raw" not in bb.get_state_value("final_answer")

    def test_aborted_exit_does_not_carry_raw(self):
        bb = Blackboard()
        bb.update_state_value("result", PLAN)
        _exit_node(bb).action_handler(Message())
        bb.update_state_value("manager_exit_kind", "aborted")
        bb.update_state_value("final_answer", {"final_answer_answer": "aborted report"})

        result = _bare_manager(bb).handle_exit()
        assert result.result_type == "manager_aborted"
        assert "final_answer_raw" not in (result.data or {})


def test_normalizer_never_lifts_final_answer_raw_into_data_list():
    normalized = FinalAnswerNormalizer.normalize({"answer": "hi", "final_answer_raw": PLAN})
    lifted_keys = {d.get("key") for d in normalized["final_answer_data_list"]}
    assert "final_answer_raw" not in lifted_keys
