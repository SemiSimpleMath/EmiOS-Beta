"""work_node_dispatch_node — carry out the switchboard's routing decision for ONE picked work node.

Reads `delegate_to` (the tool the switchboard picked for the node) plus the picked `work_id::node_id` and
hands both to the shared dispatch (node_dispatch.dispatch_node): create_dayflow_ticket -> surface a ticket
to the user (awaits their response); else -> run the node via the worker. The SAME dispatch is used by the
scheduler's precise time-wake (via
node_dispatch.route_and_dispatch), so a node routes identically whether it's picked in a tick or woken
off-tick. Then routes back to the materializer for the next ready node; each node is dispatched at most once
per tick (dispatched_this_tick guard), and the materializer short-circuits to finalize when none remain.
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MATERIALIZER = "work_node_materializer_node"


class WorkNodeDispatchNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        delegate_to = str(self.blackboard.get_state_value("delegate_to", "") or "").strip()
        acted = self.blackboard.get_state_value("acted_on_item_ids", []) or []
        work_id = node_id = None
        ref = str(acted[0]) if acted else ""
        if "::" not in ref:
            # A plain dayflow ITEM reached the work dispatch. The item lane has no dispatch tail anymore
            # (unification step C pending — the evaluator is the sole intake->action path and should have
            # converted this). Close it LOUDLY so it can't re-fire every pass; the evaluator re-mints from
            # current context if the need is still alive.
            self._close_legacy_item(ref)
            self.blackboard.update_state_value("next_agent", _MATERIALIZER)
            self.blackboard.update_state_value("last_agent", self.name)
            return
        try:
            work_id, node_id = ref.split("::", 1)
            self._guard_add(work_id, node_id)   # never re-dispatch this node within the tick
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.node_dispatch import dispatch_node
            store = get_dayflow_work_store()
            dispatch_node(store, work_id, node_id, delegate_to)
        except Exception as e:
            # Fail LOUD and FAIL THE NODE — never swallow into a silent retry. A node left ready after a
            # dispatch error re-dispatches every tick (the mechanism that turned the notify transition bug
            # into duplicate-notification spam). Mark it failed so it leaves the ready set and work_repair can
            # adjudicate it; we still drain the remaining ready nodes this tick rather than aborting.
            logger.error("[%s] node dispatch failed for %s::%s: %s",
                         self.name, work_id, node_id, e, exc_info=True)
            self._fail_node(work_id, node_id)
        # Loop back to materialize + pick the next ready node (this one is guarded / failed / done).
        self.blackboard.update_state_value("next_agent", _MATERIALIZER)
        self.blackboard.update_state_value("last_agent", self.name)

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
        adjudicates it) rather than silently re-dispatching every tick. No node to fail (unparseable ref) or
        an already-terminal node -> the ERROR log above is the loud signal."""
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
