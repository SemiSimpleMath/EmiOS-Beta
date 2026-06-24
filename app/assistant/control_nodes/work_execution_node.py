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
_NOTIFY_VALID_HOURS = 1   # ask ticket lifetime AND the in-flight re-ask interval (node wake_at = now + this)


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
        """Surface the user-reply asks — IN-FLIGHT is tracked entirely on the graph, no ticket-store dedup.
        An ask node's `wake_at` is its in-flight flag: surfacing sets wake_at = now + the re-ask interval,
        so is_ready() drops the node from ready_nodes (the orchestrator won't re-ask it); it re-appears when
        the interval lapses (re-ask), and a reply clears it. At most ONE outstanding ask per work object —
        if any of its asks is in-flight (future wake_at) we don't surface another; it waits its turn. The
        ticket is only the user-facing surface + the reply carrier, matched back by the EXACT node id.
        Replies are always recorded; per-node failures are logged and never abort the tick."""
        quiet = None   # resolved lazily, only when there is actually an ask to surface
        for work_id in active_ids:
            try:
                wo = store.load(work_id)
            except Exception:
                continue
            if str(wo.status or "").lower() in _TERMINAL_WO_STATES:
                continue
            goal_id = wo.goal_node_id
            asks = [n for n in wo.nodes.values()
                    if n.id != goal_id and getattr(n, "wake_kind", None) == "user_reply"
                    and n.status in ("proposed", "waiting")]
            if not asks:
                continue

            # (1) Record any replies (matched by the EXACT node id) — clears the in-flight wake so the
            #     node becomes runnable and the worker picks it up next.
            replied = False
            for n in asks:
                try:
                    if self._record_reply(store, work_id, n, now):
                        replied = True
                except Exception as e:
                    logger.error("[%s] reply-record failed for %s::%s: %s", self.name, work_id, n.id, e)
                    logger.debug("[%s] reply-record exception", self.name, exc_info=True)
            if replied:
                wo = store.load(work_id)
                asks = [n for n in wo.nodes.values()
                        if n.id != goal_id and getattr(n, "wake_kind", None) == "user_reply"
                        and n.status in ("proposed", "waiting")]

            # (2) One outstanding ask per work object: if any ask is in-flight (future wake_at), wait.
            if any(n.wake_at is not None and n.wake_at > now for n in asks):
                continue

            # (3) Surface the first DUE ask (deps met, not in-flight) — unless quiet hours — and flag it
            #     in-flight by setting wake_at to the re-ask time. wake_ref (the question) is preserved.
            due = [n for n in asks if wo.is_ready(n, now)]
            if not due:
                continue
            if quiet is None:
                quiet = self._in_quiet_hours()
            if quiet:
                continue
            node = due[0]
            try:
                self._surface_notification(node, f"{work_id}::{node.id}")
                store.apply("defer_node", {"work_id": work_id, "node_id": node.id, "wake_kind": "user_reply",
                                           "wake_at": now + timedelta(hours=_NOTIFY_VALID_HOURS),
                                           "wake_ref": node.wake_ref}, actor="notify_user")
                logger.info("[%s] asked user for %s::%s (in-flight until re-ask)", self.name, work_id, node.id)
            except Exception as e:
                logger.error("[%s] notify-user failed for %s::%s: %s", self.name, work_id, node.id, e)
                logger.debug("[%s] notify-user exception", self.name, exc_info=True)

    def _record_reply(self, store, work_id, node, now) -> bool:
        """If the user has replied to this node's ask — matched by the EXACT id (work_id::node_id) carried
        on the ticket's trigger_context — record the answer on the node and CLEAR its wake (in-flight flag
        + question) so the worker runs it next. Returns True if a reply was recorded. The ticket store is
        read ONLY to fetch the reply text by id; it never decides whether the node is in-flight — that is
        the node's wake_at."""
        from app.assistant.ticket_manager import get_ticket_manager
        tag = f"{work_id}::{node.id}"
        tm = get_ticket_manager()
        for t in tm.get_tickets(ticket_type="dayflow_orchestrator", suggestion_type=_NOTIFY_SUGGESTION_TYPE,
                                since_utc=now - timedelta(hours=48), limit=80):
            if (getattr(t, "trigger_context", {}) or {}).get("work_node") != tag:
                continue
            reply = (getattr(t, "user_text", "") or "").strip()
            if reply:
                content = (node.content or "").rstrip()
                store.apply("set_status", {"work_id": work_id, "node_id": node.id, "status": node.status,
                                           "content": f"{content}\n\n[User replied: {reply}]"},
                            actor="notify_user")
                store.apply("defer_node", {"work_id": work_id, "node_id": node.id, "wake_kind": None},
                            actor="notify_user")
                logger.info("[%s] user replied to %s — cleared the in-flight ask", self.name, tag)
                return True
        return False

    @staticmethod
    def _surface_notification(node, tag):
        """Phrase the node's ask into a warm, user-facing question via the ticket_builder agent (the same
        phrasing LLM create_dayflow_ticket uses), then create + surface the notification ticket tagged with
        this node, non-blocking. The reply is matched back on a later tick via trigger_context.work_node."""
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
        from app.assistant.ticket_manager import get_ticket_manager
        from app.assistant.utils.pydantic_classes import Message
        want = str(getattr(node, "wake_ref", "") or "").strip() or str(node.content or "").strip()
        brief = (f"Ask the user a question so a task can proceed. Task: {node.title}. "
                 f"What I need from them: {want}. Phrase it as a warm, direct question addressed to them.")
        formatted = CreateDayflowTicketTool._format_brief(brief)   # ticket_builder agent (the phrasing LLM)
        title = (str(formatted.get("title") or node.title or "I need your input").strip())[:80]
        message = str(formatted.get("message") or want or title).strip()
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
    def _in_quiet_hours():
        """True if the user's quiet mode is on and now falls inside its window — reuses the same setting
        the reminder handler gates proactive pings on (is_quiet_mode_active('scheduler')). A settings
        hiccup never blocks a notification."""
        try:
            from app.assistant.user_settings_manager.user_settings import get_settings_manager
            return bool(get_settings_manager().is_quiet_mode_active("scheduler"))
        except Exception:
            return False

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
