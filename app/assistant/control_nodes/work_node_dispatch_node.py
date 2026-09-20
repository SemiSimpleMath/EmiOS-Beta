"""work_node_dispatch_node — CLAIM the one work node this tick dispatches.

The gate, and only the gate. It canonicalizes the selector's pick, marks that node
`dispatched` so every other consumer sees it as taken, and publishes `work_node_ref`
for the two stages that follow. It does not call anything.

The claim opens dayflow_dispatch_manager on a session thread: its arguments node builds
the call, dayflow_tool_caller executes and records it, and work_finalizer_node judges the
result. The planning pass ends at the claim; a blocking call occupies its dispatch room.

ONE dispatch per tick: each planning pass commits one call, and `dispatched` nodes are
structurally excluded from the ready list so a later pass cannot pick the same one.
When more ready nodes remain, the work-progress signal brings the next tick in minutes.
The work-lane keys are cleared here so post_room's item-lane bookkeeping never sees a
work ref.
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
        # The claimed node, published for the stages after the claim: the arguments node builds
        # that node's tool call and the tool caller records the result back onto it. The selector's
        # echo is canonicalized above, so this is the id the graph knows, never the transcription.
        self.blackboard.update_state_value("work_node_ref", ref)
        # The work lane consumes the pick; post_room's acted-on bookkeeping is item-lane only and would
        # otherwise try to close a nonexistent item row named like a work ref.
        self.blackboard.update_state_value("acted_on_item_ids", [])
        # There is ONE path after the switchboard: claim, build the arguments, call the tool,
        # judge the result. Neither of the states below can be reached by a working system —
        # the selector offers ids off its own rendered list, and the switchboard's form makes
        # delegate_to required — so they are bugs, and a bug gets an exception, not a bypass
        # route. Routing around them would make the pipeline permanently two-shaped to
        # accommodate something that should never happen, and it would not even cover the
        # case that actually strands a node (a raise AFTER the claim).
        if "::" not in ref:
            raise ValueError(
                f"[{self.name}] dispatch was handed {ref!r}, which is not a work node. "
                f"Everything this lane dispatches is work_id::node_id."
            )
        if not delegate_to:
            work_id, node_id = ref.split("::", 1)
            self._fail_node(work_id, node_id)
            raise ValueError(
                f"[{self.name}] the switchboard named no tool for {ref}. Its form requires "
                f"delegate_to, so an empty one means the switchboard did not run or did not answer."
            )
        work_id, node_id = ref.split("::", 1)
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            store = get_dayflow_work_store()
            # THE CLAIM, and the whole job of this node. Every node that reaches this gate is
            # marked in-flight BEFORE any tool is called, so every other consumer — the ready
            # set, the portfolio, the next planning pass — sees it as taken from this moment on.
            # It used to be claimed deeper and differently per lane: the worker lane claimed
            # inside open_session, and the ticket lane surfaced the question to the user FIRST
            # and marked the node afterwards, leaving a window where the user could answer a
            # node that did not yet say it was asking.
            #
            # The CALL is not made here, and not on this thread — see the hand-off below.
            claimed = self._claim(store, work_id, node_id)
            epoch = int(claimed.nodes[node_id].payload["dispatch_epoch"])
        except Exception:
            # A rejected claimant owns nothing. In particular it must not fail the winner.
            logger.error("[%s] claim failed for %s::%s", self.name, work_id, node_id, exc_info=True)
            self.blackboard.update_state_value("work_node_ref", "")
            raise

        # HAND OFF AND END THE TICK. The node is claimed, so the graph is stable: every other
        # consumer now reads it as in-flight and plans around it. That state — not a timer — is
        # what makes it safe for the next orchestrator instance to start, so this is where the
        # planning pass stops.
        #
        # The call itself happens in its own room on its own thread (work_session.open_session),
        # which blocks for as long as the tool takes. It used to happen HERE, inline, which made
        # the tick last as long as the tool: create_dayflow_ticket holds its call open for the
        # full ask window, and the scheduler admits one tick at a time and spaces the next from
        # the previous one's FINISH — so a single unanswered notify was an hour in which nothing
        # else planned, woke, or dispatched.
        try:
            from app.assistant.dayflow_orchestrator.work_session import open_session
            open_session(store, work_id, node_id, delegate_to, expected_epoch=epoch)
        except Exception:
            logger.error("[%s] could not open the dispatch room for %s::%s",
                         self.name, work_id, node_id, exc_info=True)
            self._fail_node(work_id, node_id, expected_epoch=epoch)
            raise

        self._signal_if_more_ready(ref)
        self.blackboard.update_state_value("last_agent", self.name)

    def _claim(self, store, work_id, node_id):
        """Acquire one current ready assignment in the store's write transaction."""
        wo = store.apply("claim_task", {"work_id": work_id, "node_id": node_id}, actor="dispatch_gate")
        logger.info("[%s] claimed %s::%s -> dispatched", self.name, work_id, node_id)
        return wo

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

    def _fail_node(self, work_id, node_id, *, expected_epoch=None):
        """Best-effort: mark a node failed after a dispatch error so it leaves the ready set rather
        than silently re-dispatching every pass. The architect picks it up next tick (work_repair,
        named here originally, retired on 2026-09-16). No node to fail (unparseable ref) or an
        already-terminal node -> the ERROR log above is the loud signal."""
        if not work_id or not node_id:
            return
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            store = get_dayflow_work_store()
            wo = store.load(work_id)
            node = wo.nodes.get(node_id)
            if node is None or not wo.is_work_unit(node):
                return  # A rejected helper reference must not mutate the worker's provenance.
            if expected_epoch is None and node.status == "dispatched":
                return  # No ownership proof: never fail somebody else's call.
            store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "failed",
                        "expected_dispatch_epoch": expected_epoch}, actor="node_dispatch")
            logger.error("[%s] marked %s::%s failed after dispatch error — it leaves the ready set "
                         "and the architect picks it up next tick", self.name, work_id, node_id)
        except Exception as e2:
            logger.error("[%s] could not mark %s::%s failed: %s", self.name, work_id, node_id, e2)
