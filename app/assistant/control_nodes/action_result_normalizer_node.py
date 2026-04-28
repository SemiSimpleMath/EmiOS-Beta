from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode


class ActionResultNormalizerNode(ControlNode):
    """
    Normalize switchboard/tool outcomes into event-like payloads.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        result = self.blackboard.get_state_value("result", None)
        task = str(self.blackboard.get_state_value("task", "") or "").strip()
        action = str(self.blackboard.get_state_value("action", "") or "").strip()
        information = self.blackboard.get_state_value("information", "")
        events: List[Dict[str, Any]] = []

        # Only emit action_result events for actual dispatched actions/tools.
        # Never infer action execution from ambient room task/information context.
        if action and result is not None:
            result_info = ""
            if isinstance(result, dict):
                # Prefer the agent-facing one-liner; fall back to full answer.
                result_info = str(result.get("result_summary") or "").strip()
                if not result_info:
                    result_info = str(result.get("final_answer_answer") or "").strip()
            if not result_info:
                result_info = information if isinstance(information, str) else str(information)
            events.append(
                {
                    "event_type": "orchestrator_action_result",
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "action_type": action,
                    "task": task,
                    "result_payload": result,
                    "information": result_info,
                }
            )

        self.blackboard.update_state_value("action_result_events", events)
        self.blackboard.update_state_value("last_agent", self.name)
