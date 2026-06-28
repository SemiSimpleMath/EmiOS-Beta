"""work_node_dispatch_node — carry out the switchboard's routing decision for ONE picked work node.

The node-native replacement for the item lane's [switchboard_arguments -> tool_caller ->
action_result_normalizer -> post_room_finalize]. We do NOT reuse those 1:1 — they're dayflow-item
provenance machinery (resolve short ids, stamp items "dispatched", write action_dispatch rows), and a
node's record IS the graph. This one node reads `delegate_to` (the switchboard's read of the node) plus
the picked `work_id::node_id`, and:

  - WORK (delegate_to = run_work_node, or anything not the ticket tool): run the node via work_on — sets
    the work context, runs work_emi_team, records the result + status on the graph.
  - COMMUNICATIVE (delegate_to = create_dayflow_ticket):
      * one-way notify  -> deliver via create_work_notification, then mark the node done.
      * an ask (user_reply) -> surface the question + park the node (await reply; the reply pre-step in
        the materializer clears it on a later tick so the worker then runs it).

Then it routes back to the materializer to pick the next ready node; each node is dispatched at most once
per tick (dispatched_this_tick guard), and the materializer short-circuits to finalize when none remain.
"""
from datetime import timedelta

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MATERIALIZER = "work_node_materializer_node"
_NOTIFY_SUGGESTION_TYPE = "work_notify"
_REASK_HOURS = 1


class WorkNodeDispatchNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        acted = self.blackboard.get_state_value("acted_on_item_ids", []) or []
        delegate_to = str(self.blackboard.get_state_value("delegate_to", "") or "").strip()
        work_id = node_id = None
        try:
            ref = str(acted[0]) if acted else ""
            if "::" not in ref:
                raise ValueError(f"expected a work_id::node_id ref, got {ref!r}")
            work_id, node_id = ref.split("::", 1)
            self._guard_add(work_id, node_id)   # never re-dispatch this node within the tick
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            store = get_dayflow_work_store()
            if delegate_to == "create_dayflow_ticket":
                self._communicate(store, work_id, node_id)
            else:
                self._do_work(store, work_id, node_id)
        except Exception as e:
            # Fail LOUD and FAIL THE NODE — never swallow into a silent retry. A node left ready
            # after a dispatch error re-dispatches every tick (the mechanism that turned the notify
            # transition bug into duplicate-notification spam). Mark it failed so it leaves the ready
            # set and work_repair can adjudicate it (retry / escalate / abandon); we still drain the
            # remaining ready nodes this tick rather than aborting the whole tick.
            logger.error("[%s] node dispatch failed for %s::%s: %s",
                         self.name, work_id, node_id, e, exc_info=True)
            self._fail_node(work_id, node_id)
        # Loop back to materialize + pick the next ready node (this one is guarded / failed / done).
        self.blackboard.update_state_value("next_agent", _MATERIALIZER)
        self.blackboard.update_state_value("last_agent", self.name)

    def _fail_node(self, work_id, node_id):
        """Best-effort: mark a node failed after a dispatch error so it leaves the ready set
        (work_repair adjudicates it) rather than silently re-dispatching every tick. No node to
        fail (unparseable ref) or an already-terminal node -> the ERROR log above is the loud signal."""
        if not work_id or not node_id:
            return
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            store = get_dayflow_work_store()
            store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "failed"},
                        actor="node_dispatch")
            logger.error("[%s] marked %s::%s failed after dispatch error -> work_repair",
                         self.name, work_id, node_id)
        except Exception as e2:
            logger.error("[%s] could not mark %s::%s failed: %s", self.name, work_id, node_id, e2)

    def _guard_add(self, work_id, node_id):
        g = list(self.blackboard.get_state_value("dispatched_this_tick", []) or [])
        g.append(f"{work_id}::{node_id}")
        self.blackboard.update_state_value("dispatched_this_tick", g)

    @staticmethod
    def _do_work(store, work_id, node_id):
        from work_objects.work_runtime import work_on
        status = work_on(store, work_id, node_id=node_id)   # context + worker + result, all on the graph
        logger.info("[work_node_dispatch] work %s::%s -> %s", work_id, node_id, status)

    def _communicate(self, store, work_id, node_id):
        from work_objects.store import FAMILY_BY_TYPE
        node = store.load(work_id).nodes.get(node_id)
        if node is None:
            return
        wake_kind = str(getattr(node, "wake_kind", None) or "")
        family = FAMILY_BY_TYPE.get(getattr(node, "type", "") or "", "spine")

        # ONE-WAY NOTIFY — only a NOTIFY-FAMILY node with no reply wake: deliver + close `done` (the notify
        # family allows actionable->done). A spine WORK node the switchboard sent to the ticket side must NOT
        # be closed `done` here — actionable->done is illegal for spine (the recurring dispatch failure) AND
        # the work isn't actually finished by a notification; it falls through to the ASK path below.
        if family == "notify" and wake_kind != "user_reply":
            from app.assistant.lib.tools.create_work_notification.create_work_notification import CreateWorkNotificationTool
            if CreateWorkNotificationTool._create(work_id, node_id) is not None:
                store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "done",
                                           "content": (node.content or "")}, actor="node_dispatch")
                logger.info("[work_node_dispatch] notified + closed %s::%s", work_id, node_id)
            else:
                logger.warning("[work_node_dispatch] notify produced no ticket for %s::%s; retry next tick",
                               work_id, node_id)
            return

        # ASK — a user_reply ask, OR a spine/work node the switchboard judged needs the user (only the user
        # can do/confirm it). Surface it and park OUT of `actionable` to `waiting` (in-flight, awaiting the
        # reply; the materializer lists by status==actionable, so leaving it actionable would re-surface every
        # tick). defer_node only flips active->waiting, so set it explicitly first.
        self._surface_ask(work_id, node_id, node)
        from work_objects.model import utcnow
        store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "waiting"},
                    actor="node_dispatch")
        store.apply("defer_node", {"work_id": work_id, "node_id": node_id, "wake_kind": "user_reply",
                                   "wake_at": utcnow() + timedelta(hours=_REASK_HOURS),
                                   "wake_ref": node.wake_ref or node.title}, actor="node_dispatch")
        logger.info("[work_node_dispatch] asked owner %s::%s (parked until reply/re-ask)", work_id, node_id)

    @staticmethod
    def _surface_ask(work_id, node_id, node):
        """Phrase + surface the node's question as a decision ticket tagged with this node; the reply is
        matched back via trigger_context.work_node on a later tick. Same shape as the notify ticket but a
        decision (expects a reply)."""
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
        from app.assistant.ticket_manager import get_ticket_manager
        from app.assistant.utils.pydantic_classes import Message
        want = str(getattr(node, "wake_ref", "") or "").strip() or str(node.content or "").strip()
        brief = (f"Ask the user a question so a task can proceed. Task: {node.title}. "
                 f"What I need from them: {want}. Phrase it as a warm, direct question addressed to them.")
        formatted = CreateDayflowTicketTool._format_brief(brief)
        title = (str(formatted.get("title") or node.title or "I need your input").strip())[:80]
        message = str(formatted.get("message") or want or title).strip()
        tm = get_ticket_manager()
        ticket = tm.create_ticket(ticket_type="dayflow_orchestrator", suggestion_type=_NOTIFY_SUGGESTION_TYPE,
                                  title=title, message=message, trigger_context={"work_node": f"{work_id}::{node_id}"},
                                  valid_hours=_REASK_HOURS)
        if not ticket or not tm.mark_proposed(ticket.ticket_id):
            return
        payload = ticket.to_dict()
        payload["button_layout"] = "decision"
        payload["plan_mode_available"] = True
        DI.event_hub.publish(Message(event_topic="proactive_suggestion", data=payload))
