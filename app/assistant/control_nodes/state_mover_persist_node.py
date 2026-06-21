from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_EVENT_WAKES = {"event", "user_reply", "signal"}


class StateMoverPersistNode(ControlNode):
    """Post-guard node after state_transition_guard_node.

    state_transition_guard_node already saved the item ``state_mutations``. This node:
      1. sets ``state_mutations_persisted_tf`` so post_room_finalize_node skips re-applying them, and
      2. applies the state_mover's ``node_wakes`` — for each work-object node whose awaited external
         event arrived, clear its event-wait (so is_ready / work_execution pick it up next tick) and
         attach the arrived content as the worker's resume context.

    This is the work-object-aware half of the state_mover; it replaces the standalone event_waker.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("state_mutations_persisted_tf", True)
        try:
            self._apply_node_wakes()
        except Exception as e:
            logger.error("[%s] node_wakes apply failed: %s", self.name, e)
            logger.debug("[%s] node_wakes exception", self.name, exc_info=True)
        self.blackboard.update_state_value("last_agent", self.name)

    def _apply_node_wakes(self):
        node_wakes = self.blackboard.get_state_value("node_wakes", []) or []
        if not node_wakes:
            return
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
        woken = []
        for m in node_wakes:
            if not isinstance(m, dict):
                continue
            tid = str(m.get("task_id") or "")
            if "::" not in tid:
                continue
            work_id, node_id = tid.rsplit("::", 1)
            try:
                wo = store.load(work_id)
                node = wo.nodes.get(node_id)
                if node is None or getattr(node, "wake_kind", None) not in _EVENT_WAKES:
                    continue  # already woken, or not an event-wait
                status, content = node.status, (node.content or "")
                store.apply("defer_node", {"work_id": work_id, "node_id": node_id, "wake_kind": None},
                            actor="state_mover")
                evidence = str(m.get("evidence") or "").strip()
                if evidence:
                    new_content = content.rstrip() + f"\n\n[Awaited event arrived] {evidence}"
                    store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                                               "status": status, "content": new_content},
                                actor="state_mover")
                woken.append(tid)
                logger.info("[%s] woke work-object node %s", self.name, tid)
            except Exception as e:
                logger.warning("[%s] could not wake %s: %s", self.name, tid, e)
        if woken:
            self.blackboard.update_state_value("woken_work_nodes", woken)
