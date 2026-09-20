"""Explicit denial survives child/parent results and ordinary finalization."""
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from app.assistant.control_nodes.tool_result_handler import ToolResultHandler
from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore
from work_objects.result_recorder import record_tool_result


def test_denial_propagates_two_parent_hops_and_finalizes_stop():
    result = ToolResult(result_type="error", content="User denied the action",
        data={"abort_policy": "abort_task", "error_code": "approval_rejected",
              "retryable": False, "user_visible": True})
    for default_exit in (False, True):
        bb = FakeBlackboard()
        handler = ToolResultHandler("handler", bb, None, None)
        assert handler._maybe_abort_task_for_clearance_error(selected_tool="child", tool_result=result)
        assert bb.get_state_value("error") is True
        mgr = MultiAgentManager.__new__(MultiAgentManager)
        mgr.name = "parent"
        mgr.blackboard = bb
        bb.update_state_value("manager_exit_kind", "aborted")
        bb.update_state_value("final_answer", {"final_answer_answer": "Child stopped"})
        result = mgr.handle_default_error_exit() if default_exit else mgr.handle_exit()
        assert result.result_type == "manager_aborted"
        assert result.data["abort_policy"] == "abort_task"
        assert result.data["error_code"] == "approval_rejected"
    s = WorkStore(":memory:")
    try:
        wo = s.apply("create_work_object", {"title": "Denied task"})
        s.apply("add_node", {"work_id": wo.id, "id": "main", "type": "subtask", "parent_id": wo.goal_node_id})
        s.apply("set_status", {"work_id": wo.id, "node_id": "main", "status": "dispatched"})
        assert record_tool_result(s, wo.id, "main", result, actor="worker", expected_epoch=1)
        wo = s.apply("finalize_task", {"work_id": wo.id, "node_id": "main", "expected_dispatch_epoch": 1,
            "finalizer": {"verdict": "retry", "outcome": "Approval was denied", "next_step": "retry"}})
        fin = wo.nodes["main"].payload["finalizer"]
        assert fin["verdict"] == "unrecoverable"
        assert fin["next_step"] == "stop"
        assert not fin["question_for_user"]
    finally:
        s.close()


def test_recoverable_child_failure_does_not_abort_parent():
    handler = ToolResultHandler("handler", FakeBlackboard(), None, None)
    result = ToolResult(result_type="manager_aborted", content="Budget exhausted", data={"aborted": True})
    assert not handler._maybe_abort_task_for_clearance_error(selected_tool="child", tool_result=result)
