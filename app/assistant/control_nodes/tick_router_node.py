"""Top-of-pipeline router for the dayflow orchestrator manager.

Routes the trigger Message:

- ``triggered_work_node`` set → a TARGETED pass for that one due node: the state_mover judges
  whether now is actually a good moment, then work_node_wake_router_node dispatches it (or not)
- otherwise → intake_triage_prep_node (normal full pipeline)

THE CONTRACT for a targeted pass: **no re-planning, but always re-judge the moment.** The architect's
decision about what to do and roughly when stands and is not reopened — intake, the evaluator, the
architect and repair are all skipped. But whether RIGHT NOW is a good moment to act is a fresh
judgment every time, because the world moved since the timer was set: the user may have gone to bed
early, be in a meeting that ran long, or be away from the machine.

Until 2026-09-16 this went straight to the switchboard, so a precisely-woken node never passed the
state_mover — the only thing that can HOLD for quiet hours, a meeting, or the user being away. The
protection existed but applied only to nodes that happened to arrive through a planning tick: a 10pm
reminder fired regardless, purely because it came through the timed door.

``wake_reason`` stays in the message for logging but is not load-bearing for routing. There used to
be a third branch — a ``fast_tick`` carrying a dayflow ITEM id. The item dispatch lane is retired
(2026-09-16); everything dispatched is a work node.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TickRouterNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        data = getattr(message, "data", {}) or {}
        triggered_work_node = str(data.get("triggered_work_node") or "").strip()

        if triggered_work_node:
            self._route_work_node_wake(triggered_work_node, data)
        else:
            logger.info(
                "[%s] normal-tick path (wake_reason=%s)",
                self.name, data.get("wake_reason", ""),
            )

        self.blackboard.update_state_value("last_agent", self.name)

    def _route_work_node_wake(self, ref: str, data) -> None:
        """Send the due node into the state_mover for a timing judgment.

        The node is staged here so work_node_wake_router_node — which runs after the state_mover
        has had its say — can dispatch it without re-deriving anything. A node that vanished or is
        no longer ready exits cleanly through the finalize path.
        """
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
        self.blackboard.update_state_value("triggered_work_node", ref)
        self.blackboard.update_state_value("next_agent", "state_mover_prep_node")
        logger.info("[%s] targeted pass for work node %s — state_mover judges the moment "
                    "(wake_reason=%s)", self.name, ref, data.get("wake_reason", ""))
