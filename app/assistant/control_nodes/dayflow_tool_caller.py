"""Dayflow tool caller.

The thin wrapper that actually calls the tool the switchboard picked, through the
same ``execute_dispatch`` util `MasterRoomToolCaller` and `ChatToolCaller` use —
so a dayflow dispatch gets the dispatch-layer gates every other room gets:
`check_tool_access` with the scope's authority, the approval flow, and a
``request_context`` carrying room_id / request_id / reply_to (which is how a
user-facing tool routes its reply back to the transport that asked).

**The call blocks, and that is the design.** Every tool in every room blocks its
caller: `get_todo_tasks` for a moment, `emi_team_manager` for however long the
sub-manager runs, `create_dayflow_ticket` for as long as the question stands.
This node runs in dayflow_dispatch_manager on its own session thread. Planning
and wake passes share a separate gate and can continue while the call waits. The
dayflow-specific part is only what happens AFTER: the ToolResult becomes graph
state on the node the call was for.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.control_nodes._tool_caller_util import execute_dispatch
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolResult

logger = get_logger(__name__)

# execute_dispatch flattens the ToolResult for the room's downstream nodes: the
# result's `data` becomes the top level, its text becomes final_answer_answer, and
# an error becomes the `error` flag. The recorder reads a ToolResult's own contract
# (result_type / data.aborted / data.exit_state), so the payload is rebuilt into one
# rather than read through two different shapes.
_FLATTENED_TEXT_KEY = "final_answer_answer"


def _as_tool_result(payload: Any) -> ToolResult:
    if isinstance(payload, ToolResult):
        return payload
    if not isinstance(payload, dict):
        return ToolResult(result_type="success", content=str(payload or ""), data={})
    return ToolResult(
        result_type="error" if payload.get("error") else "success",
        content=str(payload.get(_FLATTENED_TEXT_KEY) or ""),
        data=dict(payload),
    )


class DayflowToolCaller(ControlNode):
    """Execute the picked tool, then record its result on the node."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        ref = str(self.blackboard.get_state_value("work_node_ref", "") or "").strip()
        if "::" not in ref:
            raise ValueError(
                f"[{self.name}] work_node_ref must be 'work_id::node_id', got {ref!r}."
            )
        work_id, node_id = ref.split("::", 1)

        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from work_objects.result_recorder import record_tool_result

        store = get_dayflow_work_store()
        # MY incarnation, captured before the call. A later planning pass can arrange a
        # successor dispatch after the sweeper fails this call;
        # the recorder refuses a stale epoch rather than clobbering the successor's outcome.
        node = store.load(work_id).nodes.get(node_id)
        my_epoch = self.blackboard.get_state_value("dispatch_epoch", None)
        if (node is None or node.status != "dispatched" or my_epoch is None
                or int(my_epoch) != int(node.payload.get("dispatch_epoch") or 0)):
            raise ValueError("stale or missing dispatch attempt; refusing to execute a tool")
        my_epoch = int(my_epoch)
        tool_name = str(self.blackboard.get_state_value("action") or "").strip()

        logger.info("[%s] calling %s for %s (blocking)", self.name, tool_name, ref)
        result_payload: Dict[str, Any] = execute_dispatch(
            name=self.name,
            blackboard=self.blackboard,
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
        )

        accepted = record_tool_result(
            store, work_id, node_id, _as_tool_result(result_payload),
            actor=tool_name or self.name,
            expected_epoch=my_epoch,
            evidence_title="tool result",
        )

        self.blackboard.update_state_value("dispatch_epoch", my_epoch)
        if not accepted:
            self.blackboard.update_state_value("work_node_ref", "")

        # A node just reached a result, so ask for a prompt follow-up tick. Not for the
        # verdict — the finalizer runs next in this very pass — but for the NEXT node:
        # whatever this result unblocks should dispatch within minutes rather than at the
        # ceiling tick. Latency only; a lost signal just means the scheduled tick picks it up.
        from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
        signal_work_progress(ref)

        self.blackboard.update_state_value("last_agent", self.name)
