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
        triggered_work_node = str(data.get("triggered_work_node") or "").strip()

        if triggered_work_node:
            # Targeted dispatch pass (a work node's precise time-wake fired): route the ONE
            # due node straight to the switchboard — the same routing + dispatch flow the
            # planning pass uses, inside the same room. No planning re-judgment: the wake
            # decision was made when the timer was set.
            self._route_work_node_wake(triggered_work_node, data)
        elif fast_tick and triggered_item_id:
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

    def _route_work_node_wake(self, ref: str, data) -> None:
        """Stage the due node for the switchboard (task = the node's goal) and enter the
        normal dispatch flow: switchboard -> work_node_dispatch -> finalize. A node that
        vanished or is no longer ready exits cleanly through the finalize path."""
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from work_objects.model import utcnow

        work_id, _, node_id = ref.partition("::")
        store = get_dayflow_work_store()
        try:
            wo = store.load(work_id)
        except KeyError:
            wo = None
        node = wo.nodes.get(node_id) if wo is not None else None
        if node is None or not wo.is_ready(node, utcnow()):
            logger.info(
                "[%s] work-node wake %s no longer dispatchable (%s) — clean exit.",
                self.name, ref, "missing" if node is None else f"status={node.status}",
            )
            self.blackboard.update_state_value("next_agent", "post_room_finalize_node")
            return
        task = (f"{node.title}. {node.content}" if node.content else (node.title or "")).strip()
        self.blackboard.update_state_value("task", task)
        self.blackboard.update_state_value("information", "")
        self.blackboard.update_state_value("acted_on_item_ids", [ref])
        self.blackboard.update_state_value("actionable_items", [{"item_id": ref}])
        self.blackboard.update_state_value("next_agent", "dayflow_orchestrator::switchboard")
        logger.info("[%s] targeted dispatch pass for work node %s (wake_reason=%s)",
                    self.name, ref, data.get("wake_reason", ""))
