from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class RoomSwitchboardArgumentsNode(ControlNode):
    """
    Translate switchboard output into a normalized tool call payload.

    Expected blackboard fields produced by switchboard agent:
    - delegate_to: target manager/tool name
    - task: delegated task
    - task_information: delegated supporting information (optional)
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or not last_agent.strip():
            raise ValueError(f"[{self.name}] last_agent must be set before switchboard argument mapping.")

        delegate_to = self.blackboard.get_state_value("delegate_to", "")
        task = self.blackboard.get_state_value("task", "")
        task_information = self.blackboard.get_state_value("task_information", "")

        if not isinstance(delegate_to, str) or not delegate_to.strip():
            raise ValueError(f"[{self.name}] delegate_to must be a non-empty string.")
        if task_information is None:
            task_information = ""
        if not isinstance(task_information, (str, dict)):
            raise ValueError(f"[{self.name}] task_information must be a string or object when provided.")

        selected_tool = delegate_to.strip()
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"[{self.name}] task must be a non-empty string.")

        information_value: Any
        if isinstance(task_information, dict):
            information_value = task_information
        else:
            information_value = str(task_information).strip()

        arguments = {"task": task.strip(), "information": information_value}
        if selected_tool == "run_task":
            arguments["task_query"] = task.strip()

        # Canonical keys for the tool caller node (RoomToolCaller or ToolCaller).
        self.blackboard.update_state_value("action", selected_tool)
        self.blackboard.update_state_value("action_input", arguments)
        self.blackboard.update_state_value(
            "tool_arguments",
            {
                "target_name": selected_tool,
                "arguments": arguments,
            },
        )
        # Create a dayflow item so the orchestrator knows this work is happening.
        # This prevents dayflow from duplicating work the user initiated via chat.
        dayflow_item_id = self._create_dayflow_dispatch_item(
            action_name=selected_tool,
            task_summary=task.strip(),
        )
        if dayflow_item_id:
            self.blackboard.update_state_value("_master_room_dayflow_item_id", dayflow_item_id)

        # Return control to state_map routing (room_tool_caller → response_formatter → final_answer).
        # Do NOT hardcode a specific node — let the state_map handle it.
        self.blackboard.update_state_value("calling_agent", None)
        # Let the state_map determine the next node (room_tool_caller, tool_caller, etc.)
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)

    def _create_dayflow_dispatch_item(self, *, action_name: str, task_summary: str) -> str | None:
        """Create a dispatched dayflow item so the orchestrator sees this work."""
        try:
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
            from app.assistant.utils.time_utils import get_local_timezone

            now_utc = datetime.now(timezone.utc)
            item_id = f"task:{uuid.uuid4().hex[:12]}"
            room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()

            write_dayflow_item(
                item_id,
                state="dispatched",
                updates={
                    "source_type": "plan_task",
                    "event_type": "master_room_dispatch",
                    "summary": task_summary,
                    "importance": "medium",
                    "actionability": "actionable",
                    "dispatched_at": now_utc.isoformat(),
                    "dispatched_at_local": now_utc.astimezone(get_local_timezone()).strftime("%I:%M %p"),
                    "dispatch_origin": room_id or "master_room",
                    "dispatch_action": action_name,
                },
                reason="master_room_dispatch",
                caller=f"{self.name}::master_room",
                content=task_summary,
                source_type="plan_task",
            )
            logger.info("[%s] created dayflow dispatch item %s for master_room action: %s", self.name, item_id, action_name)
            return item_id
        except Exception as e:
            logger.warning("[%s] failed to create dayflow dispatch item: %s", self.name, e)
            return None

