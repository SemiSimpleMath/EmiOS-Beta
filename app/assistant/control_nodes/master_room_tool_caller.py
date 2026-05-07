"""Master_room tool caller.

Same dispatch flow as ChatToolCaller (via shared ``execute_dispatch`` util)
plus the master_room-specific step of closing the dayflow dispatch marker
that MasterRoomSwitchboardArgumentsNode created before the tool ran.

Marker close is master_room-only — slack/sms/telegram never wrote a
marker, dayflow has its own provenance via post_room_finalize.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.control_nodes._tool_caller_util import execute_dispatch
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class MasterRoomToolCaller(ControlNode):
    """Execute dispatch, then close the master_room dayflow marker."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        result_payload = execute_dispatch(
            name=self.name,
            blackboard=self.blackboard,
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
        )

        marker_id = self.blackboard.get_state_value("_master_room_dayflow_item_id")
        if marker_id:
            self._close_dayflow_dispatch_marker(marker_id, result_payload)

        self.blackboard.update_state_value("last_agent", self.name)

    @staticmethod
    def _close_dayflow_dispatch_marker(
        item_id: str, result_payload: Dict[str, Any] | str,
    ) -> None:
        """Stamp execution result and close the dispatched dayflow_item the
        master_room arguments node created. Best-effort: never blocks the
        dispatch flow from returning normally.

        Uses dayflow_orchestrator::result_formatter to compress the full
        manager result into a 1-3 line task-update line. NO TRUNCATION
        of the source — the formatter agent reads the full text and
        decides what's important. See feedback_no_truncated_tool_results.md.
        """
        try:
            from datetime import datetime, timezone
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import (
                write_dayflow_item,
            )
            from app.assistant.control_nodes.post_room_finalize_node import (
                _extract_full_result_text,
                _format_compact_outcome,
            )

            full_text = ""
            if isinstance(result_payload, dict):
                full_text = _extract_full_result_text(
                    {"result_payload": result_payload}
                )
            else:
                full_text = str(result_payload or "")

            execution_result = _format_compact_outcome(
                task="",
                action="master_room_dispatch",
                full_result=full_text,
            ) or "Completed."

            write_dayflow_item(
                item_id,
                state="closed",
                updates={
                    "execution_result": execution_result,
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                },
                reason="master_room_dispatch_completed",
                caller="master_room_tool_caller",
            )
            logger.info(
                "[master_room_tool_caller] closed dayflow dispatch marker %s",
                item_id,
            )
        except Exception as e:
            logger.error(
                "[master_room_tool_caller] failed to close dayflow dispatch marker %s: %s",
                item_id, e, exc_info=True,
            )
