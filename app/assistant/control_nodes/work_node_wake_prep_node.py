"""Head of the WAKE PASS (dayflow_wake_manager): stage the ONE due node for the state_mover.

A work node's precise time-wake fired. This pass exists to answer one question — is now a good
moment to run it — and then dispatch it or hold it. It never plans: there is no intake, steward
or architect in this manager at all.

What this node does:
  * reads `triggered_work_node` off the blackboard (the manager copies the trigger's data there;
    the activation Message a control node receives carries none of it — reading the Message is
    how the old in-orchestrator wake branch silently never fired, 09-16..09-18);
  * loads the node; if it is gone or no longer ready (another pass held or dispatched it while
    this one waited on the scheduler's run gate) the pass ends cleanly;
  * otherwise stages `task` / `information` for the switchboard, and the state_mover's view:
    `ready_work_nodes` = exactly this node, no event waits, plus the same presence / chat / ticket
    context the planning tick gives it, so a hold is judged on the same facts either way.

The state_mover's persist node honours `triggered_work_node` too: it promotes or parks THIS node
only, never the rest of the graph.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class WorkNodeWakePrepNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        ref = str(self.blackboard.get_state_value("triggered_work_node", "") or "").strip()
        if "::" not in ref:
            # This manager is only ever opened for a wake. A missing trigger is a caller bug, and a
            # pass that quietly did nothing would hide it.
            raise ValueError(f"[{self.name}] dayflow_wake_manager opened without a triggered_work_node")

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
            logger.info("[%s] wake %s no longer dispatchable (%s) — clean exit.", self.name, ref,
                        "missing" if node is None else f"status={node.status}")
            self.blackboard.update_state_value("next_agent", "post_room_finalize_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        task = (f"{node.title}. {node.content}" if node.content else (node.title or "")).strip()
        self.blackboard.update_state_value("task", task)
        self.blackboard.update_state_value("information", "")

        # The state_mover's view: one candidate, no event waits, same situational context as a tick.
        from app.assistant.control_nodes.state_mover_prep_node import recent_user_context
        from app.assistant.dayflow_orchestrator.blackboard_builder import build_dayflow_blackboard_extras
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        from app.assistant.dayflow_orchestrator.work_portfolio import STATUS_LEGEND

        now_utc = datetime.now(timezone.utc)
        chat_history, responded = recent_user_context(get_dayflow_items(), now_utc)
        self.blackboard.update_state_value("ready_work_nodes", [{
            "task_id": ref,
            "context": (node.title or node.content or "").strip().replace("\n", " ")[:140],
            "kind": str(getattr(node, "type", "") or ""),
        }])
        self.blackboard.update_state_value("waiting_work_nodes", [])
        self.blackboard.update_state_value("work_wait_intake", [])
        self.blackboard.update_state_value("node_status_legend", STATUS_LEGEND)
        self.blackboard.update_state_value("recent_dayflow_chat_history", chat_history)
        self.blackboard.update_state_value("recent_responded_tickets", responded)
        for key, value in build_dayflow_blackboard_extras().items():
            self.blackboard.update_state_value(key, value)

        logger.info("[%s] wake %s staged — the state_mover judges the moment (wake_reason=%s)",
                    self.name, ref, self.blackboard.get_state_value("wake_reason", ""))
        self.blackboard.update_state_value("last_agent", self.name)
