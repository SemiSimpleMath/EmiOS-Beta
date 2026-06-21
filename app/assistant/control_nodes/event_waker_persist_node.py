"""Post-LLM persist for the event_waker (Increment 3).

Applies the waker's matches: for each parked task whose awaited event has arrived, clear the node's
event-wait (defer_node wake_kind=None -> it stops being skipped and is_ready picks it up) and attach the
arrived content to the node (so the worker resuming it has the reply/result). work_execution_node then
runs it this same tick. Uses only existing substrate ops. Never raises.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_EVENT_WAKES = {"event", "user_reply", "signal"}


class EventWakerPersistNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        woken = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            store = get_dayflow_work_store()
            matches = self.blackboard.get_state_value("matches", []) or []
            for m in matches:
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
                    # clear the event-wait -> is_ready/execution stop holding it
                    store.apply("defer_node", {"work_id": work_id, "node_id": node_id, "wake_kind": None},
                                actor="event_waker")
                    # attach the arrived content (same-status set_status carries a content update)
                    evidence = str(m.get("evidence") or "").strip()
                    if evidence:
                        new_content = content.rstrip() + f"\n\n[Awaited event arrived] {evidence}"
                        store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                                                   "status": status, "content": new_content},
                                    actor="event_waker")
                    woken.append(tid)
                    logger.info("[%s] woke %s", self.name, tid)
                except Exception as e:
                    logger.warning("[%s] could not wake %s: %s", self.name, tid, e)
        except Exception as e:
            logger.error("[%s] persist failed: %s", self.name, e)
            logger.debug("[%s] persist exception", self.name, exc_info=True)

        self.blackboard.update_state_value("woken_tasks", woken)
        logger.info("[%s] woke %d task(s)", self.name, len(woken))
        self.blackboard.update_state_value("last_agent", self.name)
