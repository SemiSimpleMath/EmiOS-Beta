"""Execution node for the work-object dayflow pipeline.

Runs AFTER work_architect_node has laid down each goal's DAG. Two passes per tick:

PASS 1 — the NOTIFY-USER path. For every active work object's READY user_reply node it either records a
reply the user has given (writing it onto the node and clearing the wait, so the worker runs the node
next) or, if no notification is still live, surfaces one asking for that input. An unanswered node is
NEVER cleared — its notification simply expires and PASS 1 re-asks on a later tick (the retry cadence).
Deterministic + idempotent (state-throttled), so it is NOT bounded by _MAX_NODES_PER_TICK.

PASS 2 — runs the READY work nodes one at a time via work_on(...), re-evaluating after each so a chain can
progress within a tick, bounded by _MAX_NODES_PER_TICK. It SKIPS event-waits: event/signal are the
state_mover's to wake, and user_reply is handled by PASS 1. Time-gates and dependencies are enforced by
the graph. Never raises — a failure degrades to fewer nodes run this tick.

Inert until the dayflow manager's state_map routes to it.
"""
from datetime import timedelta

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MAX_NODES_PER_TICK = 5
_TERMINAL_WO_STATES = {"done", "abandoned"}
_EVENT_WAKES = {"event", "user_reply", "signal"}   # PASS 2 skips all of these
_NOTIFY_SUGGESTION_TYPE = "work_notify"
_NOTIFY_VALID_HOURS = 1            # a notification lives this long; unanswered, it expires and PASS 1 re-asks
_LIVE_TICKET_STATES = {"proposed", "pending"}


class WorkExecutionNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        executed = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from work_objects.work_runtime import work_on
            from work_objects.model import utcnow
            store = get_dayflow_work_store()
            now = utcnow()
            active_ids = [s["id"] for s in store.list_work_objects()
                          if str(s.get("status") or "").lower() not in _TERMINAL_WO_STATES]

            # PASS 1 — notify-user path (cheap, idempotent, unbounded).
            self._notify_user_pass(store, active_ids, now)

            # PASS 2 — run ready work nodes (bounded).
            done = set()   # (work_id, node_id) already run THIS tick — never re-run within a tick
            ran = 0
            while ran < _MAX_NODES_PER_TICK:
                progressed = False
                for work_id in active_ids:
                    if ran >= _MAX_NODES_PER_TICK:
                        break
                    try:
                        wo = store.load(work_id)
                    except Exception as e:
                        logger.warning("[%s] work object %s not loadable: %s", self.name, work_id, e)
                        continue
                    if str(wo.status or "").lower() in _TERMINAL_WO_STATES:
                        continue
                    node = self._next_runnable(wo, now, work_id, done)
                    if node is None:
                        continue
                    done.add((work_id, node.id))
                    try:
                        status = work_on(store, work_id, node_id=node.id)
                        executed.append({"work_id": work_id, "node": node.id, "status": status})
                        ran += 1
                        progressed = True
                        logger.info("[%s] ran node %s/%s -> %s", self.name, work_id, node.id, status)
                    except Exception as e:
                        logger.error("[%s] work_on failed for %s/%s: %s", self.name, work_id, node.id, e)
                        logger.debug("[%s] work_on exception", self.name, exc_info=True)
                        ran += 1   # count it so a failing node can't spin the loop
                if not progressed:
                    break
        except Exception as e:
            logger.error("[%s] execution node failed: %s", self.name, e)
            logger.debug("[%s] execution node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_execution_result", executed)
        logger.info("[%s] ran %d node(s)", self.name, len(executed))
        self.blackboard.update_state_value("last_agent", self.name)

    # ----------------------------------------------------------------- notify-user path

    def _notify_user_pass(self, store, active_ids, now):
        """Run the notify-user path for every active work object's READY user_reply node. Per-node
        failures are logged and never abort the tick."""
        for work_id in active_ids:
            try:
                wo = store.load(work_id)
            except Exception:
                continue
            if str(wo.status or "").lower() in _TERMINAL_WO_STATES:
                continue
            goal_id = wo.goal_node_id
            for n in wo.ready_nodes(now):
                if n.id == goal_id or getattr(n, "wake_kind", None) != "user_reply":
                    continue
                try:
                    self._notify_user_for_node(store, work_id, n, now)
                except Exception as e:
                    logger.error("[%s] notify-user failed for %s/%s: %s", self.name, work_id, n.id, e)
                    logger.debug("[%s] notify-user exception", self.name, exc_info=True)

    def _notify_user_for_node(self, store, work_id, node, now):
        """THE NOTIFY-USER PATH for one node. (1) If the user has replied to a notification for this node,
        record the reply on the node and clear the wait — PASS 2 / the next tick runs it with the answer.
        (2) Else if a notification is still live (unexpired), wait. (3) Else surface a new one. The node is
        never cleared until answered; an unanswered notification expires and gets re-asked."""
        from app.assistant.ticket_manager import get_ticket_manager
        tag = f"{work_id}::{node.id}"
        tm = get_ticket_manager()
        mine = [t for t in tm.get_tickets(ticket_type="dayflow_orchestrator",
                                          suggestion_type=_NOTIFY_SUGGESTION_TYPE,
                                          since_utc=now - timedelta(hours=48), limit=80)
                if (getattr(t, "trigger_context", {}) or {}).get("work_node") == tag]

        # (1) Replied? Record the answer on the node and clear the wait.
        for t in mine:
            reply = (getattr(t, "user_text", "") or "").strip()
            if reply:
                content = (node.content or "").rstrip()
                store.apply("set_status", {"work_id": work_id, "node_id": node.id, "status": node.status,
                                           "content": f"{content}\n\n[User replied: {reply}]"},
                            actor="notify_user")
                store.apply("defer_node", {"work_id": work_id, "node_id": node.id, "wake_kind": None},
                            actor="notify_user")
                logger.info("[%s] user replied to %s — cleared the wait", self.name, tag)
                return

        # (2) A notification still live (proposed/pending and unexpired)? Wait — don't re-ask yet.
        for t in mine:
            state = str(getattr(t, "state", "") or "").lower()
            valid_until = getattr(t, "valid_until", None)
            if state in _LIVE_TICKET_STATES and (valid_until is None or valid_until > now):
                return

        # (3) Nothing live — surface a notification (the previous expired unanswered, or there is none).
        self._surface_notification(node, tag)
        content = (node.content or "").rstrip()
        store.apply("set_status", {"work_id": work_id, "node_id": node.id, "status": node.status,
                                   "content": f"{content}\n\n[Notified user; awaiting reply, will re-ask if unanswered.]"},
                    actor="notify_user")
        logger.info("[%s] notified user for %s", self.name, tag)

    @staticmethod
    def _surface_notification(node, tag):
        """Create + surface a notification ticket tagged with this node, non-blocking. The reply is matched
        back on a later tick via trigger_context.work_node."""
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.ticket_manager import get_ticket_manager
        from app.assistant.utils.pydantic_classes import Message
        title = (str(getattr(node, "title", "") or "").strip() or "I need your input")[:80]
        parts = [p for p in [str(node.content or "").strip(),
                             (f"Specifically, I need: {node.wake_ref}" if getattr(node, "wake_ref", "") else "")]
                 if p]
        message = " ".join(parts) or title
        tm = get_ticket_manager()
        ticket = tm.create_ticket(ticket_type="dayflow_orchestrator", suggestion_type=_NOTIFY_SUGGESTION_TYPE,
                                  title=title, message=message, trigger_context={"work_node": tag},
                                  valid_hours=_NOTIFY_VALID_HOURS)
        if not ticket or not tm.mark_proposed(ticket.ticket_id):
            return
        payload = ticket.to_dict()
        payload["button_layout"] = "decision"     # expects a reply (matches create_dayflow_ticket's decision kind)
        payload["plan_mode_available"] = True
        DI.event_hub.publish(Message(event_topic="proactive_suggestion", data=payload))

    @staticmethod
    def _next_runnable(wo, now, work_id, done):
        """The first ready node worth running in PASS 2: not the goal node, not already run this tick, and
        not an event-wait (event/signal -> state_mover; user_reply -> PASS 1). Time/dependency gates are
        already enforced by wo.ready_nodes()."""
        goal_id = wo.goal_node_id
        for n in wo.ready_nodes(now):
            if n.id == goal_id:
                continue
            if (work_id, n.id) in done:
                continue
            if getattr(n, "wake_kind", None) in _EVENT_WAKES:
                continue
            return n
        return None
