"""Top-of-pipeline router for the dayflow orchestrator manager.

Reads the trigger Message's ``data.fast_tick`` flag and routes accordingly:

- ``fast_tick=True`` + valid ``triggered_item_id`` → fast_tick_promoter_node
  (deterministic single-item wake, skips intake_triage, planner, state_mover)
- otherwise → intake_triage_prep_node (normal full pipeline)

The routing flag is typed (boolean) to avoid string-literal coupling between
the scheduler and the manager. ``wake_reason`` stays in the message for
logging but is not load-bearing for routing.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TickRouterNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        data = getattr(message, "data", {}) or {}
        fast_tick = bool(data.get("fast_tick"))
        triggered_item_id = str(data.get("triggered_item_id") or "").strip()

        if fast_tick and triggered_item_id:
            self.blackboard.update_state_value("triggered_item_id", triggered_item_id)
            self.blackboard.update_state_value("next_agent", "fast_tick_promoter_node")
            logger.info(
                "[%s] fast-tick path for item %s (wake_reason=%s)",
                self.name, triggered_item_id, data.get("wake_reason", ""),
            )
        else:
            logger.info(
                "[%s] normal-tick path (wake_reason=%s)",
                self.name, data.get("wake_reason", ""),
            )

        self.blackboard.update_state_value("last_agent", self.name)
