"""work_node_materializer_node — build the actionable list from READY work-object nodes.

The work-object analogue of view_materializer (which built the list from dayflow items). Each ready node
becomes ONE actionable item (item_id = "work_id::node_id", summary = the node's task) — exactly the shape
action_selector consumes. The switchboard then reads each and routes it (notify -> create_dayflow_ticket,
work -> run_work_node). One node is picked per cycle; the manager loops back here for the next until none
are ready, then short-circuits to finalize.

Excludes: the goal node; event/signal-waiting nodes (the state_mover wakes those); and not-yet-ready /
in-flight nodes (future wake_at, e.g. an ask already surfaced and awaiting a reply). Includes ready work,
notify, and non-in-flight ask (user_reply) nodes.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TERMINAL_WO_STATES = {"done", "abandoned"}


class WorkNodeMaterializerNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        items = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from work_objects.model import utcnow
            store = get_dayflow_work_store()
            now = utcnow()
            guard = set(self.blackboard.get_state_value("dispatched_this_tick", []) or [])
            for s in store.list_work_objects():
                if str(s.get("status") or "").lower() in _TERMINAL_WO_STATES:
                    continue
                # Reply pre-step: an ask's reply is its result — record it as evidence + complete the node
                # (-> done) so the finalizer judges it. Idempotent (a completed ask is no longer user_reply).
                try:
                    self._record_replies(store, s["id"], now)
                except Exception as e:
                    logger.warning("[%s] reply-read failed for %s: %s", self.name, s.get("id"), e)
                try:
                    wo = store.load(s["id"])
                except Exception as e:
                    logger.warning("[%s] work object %s not loadable: %s", self.name, s.get("id"), e)
                    continue
                for item in self._node_items(wo, now):
                    if item["item_id"] not in guard:   # never re-list a node already dispatched this tick
                        items.append(item)
        except Exception as e:
            logger.error("[%s] work-node materialize failed: %s", self.name, e)
            logger.debug("[%s] materialize exception", self.name, exc_info=True)

        self.blackboard.update_state_value("actionable_items", items)
        # Mirror view_materializer's empty short-circuit: nothing ready -> skip action_selector.
        if not items:
            logger.info("[%s] no ready work nodes — skipping action_selector.", self.name)
            self.blackboard.update_state_value("next_agent", "post_room_finalize_node")
        logger.info("[%s] materialized %d ready work node(s)", self.name, len(items))
        self.blackboard.update_state_value("last_agent", self.name)

    @staticmethod
    def _node_items(wo, now):
        """The ACTIONABLE nodes of ONE work object, as action_selector items. A node is dispatchable ONLY
        once state_mover has promoted it (proposed/waiting -> actionable); a node still in proposed/waiting
        sits parked in the architect's inbox and is NOT listed here. (External-event nodes are never promoted
        — the state_mover wakes them via node_wakes — so they never reach actionable.)"""
        out = []
        goal_id = wo.goal_node_id
        for n in wo.nodes.values():
            if n.id == goal_id:
                continue
            if n.status != "actionable":
                continue
            task = (f"{n.title}. {n.content}" if n.content else (n.title or "")).strip()
            out.append({
                "item_id": f"{wo.id}::{n.id}",
                "short_id": f"{wo.id}::{n.id}",
                "summary": task,
                "source_type": "work_node",
                "state": "actionable",
                "importance": "medium",
                "actionability": "actionable",
            })
        return out

    def _record_replies(self, store, work_id, now):
        """Match user replies back to this work object's ask nodes (by trigger_context.work_node). An ask is
        just a tool call whose result arrives asynchronously: when the reply lands it IS the node's outcome,
        so record it as EVIDENCE (never mutate the directive) and complete the node -> done. The finalizer
        then judges the reply like any other tool result — an answer -> PROCEED (unblock the dependents that
        use it); "stop / not now / I'll handle it" -> AMEND (drop that line) or RESOLVE->abandon. We do NOT
        clear the wait and re-run the worker; that re-asks a declined ask forever. Read-only on the ticket
        store (it carries the reply text)."""
        from datetime import timedelta
        from app.assistant.ticket_manager import get_ticket_manager
        from work_objects.model import new_id
        wo = store.load(work_id)
        goal_id = wo.goal_node_id
        asks = [n for n in wo.nodes.values()
                if n.id != goal_id and getattr(n, "wake_kind", None) == "user_reply"
                and n.status in ("proposed", "waiting")]
        if not asks:
            return
        tm = get_ticket_manager()
        replies = {}
        for t in tm.get_tickets(ticket_type="dayflow_orchestrator", suggestion_type="work_notify",
                                since_utc=now - timedelta(hours=48), limit=80):
            tag = (getattr(t, "trigger_context", {}) or {}).get("work_node")
            reply = (getattr(t, "user_text", "") or "").strip()
            if tag and reply:
                replies[tag] = reply
        for n in asks:
            reply = replies.get(f"{work_id}::{n.id}")
            if not reply:
                continue
            # A spine ask can't go proposed->done directly; hop via dispatched (legal for spine + notify).
            if n.status == "proposed":
                store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "dispatched"}, actor="reply")
            store.apply("add_node", {"work_id": work_id, "id": new_id("reply"), "type": "evidence",
                                     "parent_id": n.id, "status": "assumed", "created_by": "reply",
                                     "title": "user reply", "content": reply}, actor="reply")
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "done"}, actor="reply")
            logger.info("[%s] reply recorded as result for %s::%s — node done -> finalizer judges it",
                        self.name, work_id, n.id)
