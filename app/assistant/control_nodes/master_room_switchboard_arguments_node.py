"""Master_room switchboard arguments node.

Master_room has a unique relationship with the dayflow orchestrator: when
the user dispatches a tool through master_room chat, we write a "dispatched"
marker into the dayflow_item table so the orchestrator sees in-flight chat
work and (in principle) doesn't duplicate it. Other chat surfaces
(slack / sms / telegram) do NOT do this — they use ChatSwitchboardArgumentsNode.

Dayflow's own switchboard dispatches (DayflowSwitchboardArgumentsNode) write
their own provenance via _persist_dispatch_records — they do not use this
node and do not write the master_room marker.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.assistant.control_nodes._switchboard_arguments_util import (
    normalize_switchboard_args,
)
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class MasterRoomSwitchboardArgumentsNode(ControlNode):
    """Translate switchboard output + write a dayflow dispatch marker.

    Same input/output contract as ChatSwitchboardArgumentsNode (the
    blackboard key set after this node runs is identical). The extra step
    is the marker write: a dispatched dayflow_item row that pairs with the
    close performed by MasterRoomToolCaller after the tool returns.

    Marker intent: anti-duplication. If dayflow's planner runs concurrently
    and considers planning the same work the user just asked master_room to
    do, the marker should help it skip. (Effectiveness has been mixed —
    see project notes; concept retained intentionally pending review.)
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        arguments = normalize_switchboard_args(self.blackboard, name=self.name)

        selected_tool = str(self.blackboard.get_state_value("action") or "").strip()
        task_text = str(arguments.get("task") or "").strip()

        marker_id = self._create_dayflow_dispatch_marker(
            action_name=selected_tool, task_summary=task_text,
        )
        if marker_id:
            self.blackboard.update_state_value("_master_room_dayflow_item_id", marker_id)

        self.blackboard.update_state_value("calling_agent", None)
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)

    def _create_dayflow_dispatch_marker(
        self, *, action_name: str, task_summary: str,
    ) -> str | None:
        """Write a dispatched dayflow_item row tracking this master_room
        dispatch. MasterRoomToolCaller closes it after the tool returns.

        Best-effort: returns None on failure so the dispatch itself still
        proceeds even if the marker write fails.
        """
        try:
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import (
                write_dayflow_item,
            )
            from app.assistant.utils.time_utils import get_local_timezone

            now_utc = datetime.now(timezone.utc)
            item_id = f"task:{uuid.uuid4().hex[:12]}"
            room_id = str(
                self.blackboard.get_state_value("room_id", "") or ""
            ).strip() or "master_room"

            write_dayflow_item(
                item_id,
                state="dispatched",
                updates={
                    "source_type": "master_room_dispatch",
                    "event_type": "master_room_dispatch",
                    "summary": task_summary,
                    "importance": "medium",
                    "actionability": "actionable",
                    "dispatched_at": now_utc.isoformat(),
                    "dispatched_at_local": now_utc.astimezone(
                        get_local_timezone()
                    ).strftime("%I:%M %p"),
                    "dispatch_origin": room_id,
                    "dispatch_action": action_name,
                },
                reason="master_room_dispatch",
                caller=f"{self.name}::master_room",
                content=task_summary,
                source_type="master_room_dispatch",
            )
            logger.info(
                "[%s] created dayflow dispatch marker %s for master_room action: %s",
                self.name, item_id, action_name,
            )
            return item_id
        except Exception as e:
            logger.error(
                "[%s] failed to create dayflow dispatch marker: %s",
                self.name, e, exc_info=True,
            )
            return None
