"""work_node_dispatch_node — carry out the switchboard's routing decision for THE ONE work node this
tick dispatches.

Reads `delegate_to` (the tool the switchboard picked for the node) plus the picked `work_id::node_id` and
hands both to the shared dispatch (node_dispatch.dispatch_node): create_dayflow_ticket -> surface a ticket
to the user (awaits their response); else -> the worker runs on its OWN job thread and this tick moves on.
The SAME dispatch is used by the scheduler's precise time-wake (node_dispatch.route_and_dispatch), so a
node routes identically whether it's picked in a tick or woken off-tick.

ONE dispatch per tick — the master_room model: each planning pass commits one job; concurrency comes from
job threads overlapping ACROSS ticks, with every pass seeing the in-flight state (`dispatched` nodes are
structurally excluded from the ready list). When more ready nodes remain, the work-progress signal brings
the next tick in minutes. The tick then proceeds to finalize (the state_map routes onward); the work-lane
keys are cleared here so post_room's item-lane bookkeeping never sees a work ref.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class WorkNodeDispatchNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        delegate_to = str(self.blackboard.get_state_value("delegate_to", "") or "").strip()
        acted = self.blackboard.get_state_value("acted_on_item_ids", []) or []
        work_id = node_id = None
        ref = self._canonicalize_ref(str(acted[0]) if acted else "")
        # The work lane consumes the pick; post_room's acted-on bookkeeping is item-lane only and would
        # otherwise try to close a nonexistent item row named like a work ref.
        self.blackboard.update_state_value("acted_on_item_ids", [])
        if "::" not in ref:
            # A plain dayflow ITEM reached the work dispatch. The item lane has no dispatch tail anymore
            # (unification step C pending — the evaluator is the sole intake->action path and should have
            # converted this). Close it LOUDLY so it can't re-fire every pass; the evaluator re-mints from
            # current context if the need is still alive.
            self._close_legacy_item(ref)
        else:
            try:
                work_id, node_id = ref.split("::", 1)
                from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
                from app.assistant.dayflow_orchestrator.node_dispatch import dispatch_node
                store = get_dayflow_work_store()
                dispatch_node(store, work_id, node_id, delegate_to)
            except Exception as e:
                # Fail LOUD and FAIL THE NODE — never swallow into a silent retry. A node left ready after
                # a dispatch error re-dispatches every pass (the mechanism that turned the notify
                # transition bug into duplicate-notification spam). Mark it failed so it leaves the ready
                # set and work_repair adjudicates it next tick.
                logger.error("[%s] node dispatch failed for %s::%s: %s",
                             self.name, work_id, node_id, e, exc_info=True)
                self._fail_node(work_id, node_id)
        self._signal_if_more_ready(ref)
        self.blackboard.update_state_value("last_agent", self.name)

    def _canonicalize_ref(self, ref):
        """The selector ECHOES an id from its rendered list, and echoes arrive decorated —
        the prompt's "- task: " label glued on ("task:work_x::node_y"), stray whitespace.
        Join the echo back to the offered set (the item_ids the runtime itself put on the
        blackboard) and dispatch the CANONICAL id, never the transcription. No unique
        match -> return the echo unchanged, so the loud dispatch failure below stands
        (sink, not drop). This also keeps _fail_node working: a glued work_id used to
        poison the failure path too, leaving the node ready to re-fire every pass."""
        ref = (ref or "").strip()
        if not ref or "::" not in ref:
            return ref
        items = self.blackboard.get_state_value("actionable_items", []) or []
        offered = [str(i.get("item_id") or "") for i in items
                   if isinstance(i, dict) and "::" in str(i.get("item_id") or "")]
        if ref in offered:
            return ref
        matches = [o for o in offered if o in ref or ref in o]
        if len(matches) == 1:
            logger.warning("[%s] canonicalized selector ref %r -> %r", self.name, ref, matches[0])
            return matches[0]
        return ref

    def _signal_if_more_ready(self, dispatched_ref):
        """More ready work nodes were listed this tick beyond the one dispatched — ask the scheduler for
        a prompt follow-up tick so the next one dispatches in minutes, not at the ceiling."""
        try:
            items = self.blackboard.get_state_value("actionable_items", []) or []
            remaining = [i for i in items
                         if "::" in str(i.get("item_id") or "") and i.get("item_id") != dispatched_ref]
            if remaining:
                from app.assistant.dayflow_orchestrator.node_dispatch import signal_work_progress
                signal_work_progress(f"more_ready:{len(remaining)}")
        except Exception as e:
            logger.warning("[%s] more-ready signal failed: %s", self.name, e)

    def _close_legacy_item(self, ref):
        """Close a legacy dayflow item that reached the work dispatch (the retired lane's tail). Loud by
        design: this firing at all means the evaluator left an actionable item unconverted."""
        logger.error(
            "[%s] LEGACY ITEM LANE reached dispatch with item %r — closing it "
            "(reason=legacy_item_lane_dispatch_retired); the evaluator did not convert this intake.",
            self.name, ref)
        if not ref:
            return
        try:
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import (
                resolve_short_id, write_dayflow_item,
            )
            write_dayflow_item(resolve_short_id(ref), state="closed",
                               reason="legacy_item_lane_dispatch_retired", caller=self.name)
        except Exception as e:
            logger.error("[%s] could not close legacy item %r: %s", self.name, ref, e)

    def _fail_node(self, work_id, node_id):
        """Best-effort: mark a node failed after a dispatch error so it leaves the ready set (work_repair
        adjudicates it) rather than silently re-dispatching every pass. No node to fail (unparseable ref)
        or an already-terminal node -> the ERROR log above is the loud signal."""
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
