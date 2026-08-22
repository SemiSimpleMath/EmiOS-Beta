"""Control node: the WORK FINALIZER pass — reconcile each freshly-completed node's result into its whole
work object. AUTHORITATIVE (step 3): it is the only thing that produces the `closed` terminal.

Runs BEFORE the architect each tick. For every TOP-LEVEL node (a direct child of the goal — the architect's
units; the worker's nested checklist is its own business and never counts toward the goal) that the worker
left at `done`, it invokes the work_finalizer adjudicator on {the WO projection + that node's FULL,
un-truncated result} and applies the verdict:

  - PROCEED  -> close the node (`done` -> `closed`). It now counts toward the goal (is_satisfied keys on
               `closed`), so its dependents unblock and, when all the goal's children are closed, the rollup
               completes the WorkObject.
  - AMEND    -> close the node + flag the WO for re-plan (append to `replan_work_ids`) and stash the revised
               intent in `finalizer_amend_intents`; the architect (next node in the state_map) revises the
               graph from it.
  - RESOLVE  -> the goal itself is settled: `close` -> close the node + set the WorkObject done; `abandon`
               -> set the WorkObject abandoned.

A worker-`done` node is NOT satisfied on its own anymore (is_satisfied -> `closed`), so nothing closes a goal
until the finalizer has judged its results — this is what removes the blind auto-completion. Bounded per
tick; never raises.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_MAX_FINALIZE_PER_TICK = 5
_TERMINAL_WO = {"done", "abandoned"}


class WorkFinalizerNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        applied, replan_ids, amend_intents = [], [], {}
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            store = get_dayflow_work_store()

            candidates = []   # (wo, node) — top-level done nodes awaiting the finalizer's judgment
            for s in store.list_work_objects():
                if str(s.get("status") or "").lower() in _TERMINAL_WO:
                    continue
                try:
                    wo = store.load(s["id"])
                except Exception:
                    continue
                gid = wo.goal_node_id
                for n in wo.nodes.values():
                    if n.id != gid and n.parent_id == gid and n.status == "done":
                        candidates.append((wo, n))

            if candidates:
                scope = self._scope(message)
                for wo, node in candidates[:_MAX_FINALIZE_PER_TICK]:
                    try:
                        data = self._judge(wo, node, scope)
                        verdict = str(data.get("verdict") or "").strip().lower()
                        if not verdict:
                            continue
                        res = self._apply(store, wo, node, data, verdict)
                        if res.get("replan"):
                            replan_ids.append(wo.id)
                            amend_intents[wo.id] = res.get("amend_intent", "")
                        applied.append({"work_id": wo.id, "node_id": node.id, "verdict": verdict})
                        logger.info("[work_finalizer] %s::%s -> %s | %s", wo.id, node.id, verdict, res.get("note", ""))
                    except Exception as e:
                        logger.error("[%s] finalize failed for %s::%s: %s", self.name, wo.id, node.id, e)
                        logger.debug("[%s] finalize exception", self.name, exc_info=True)
        except Exception as e:
            logger.error("[%s] work_finalizer node failed: %s", self.name, e)
            logger.debug("[%s] work_finalizer node exception", self.name, exc_info=True)

        # Feed the architect (next in the state_map): merge the finalizer's re-plans into replan_work_ids
        # (the evaluator persist already set some) and stash the revised intents the architect re-plans from.
        if replan_ids:
            existing = self.blackboard.get_state_value("replan_work_ids", []) or []
            self.blackboard.update_state_value("replan_work_ids", list(dict.fromkeys([*existing, *replan_ids])))
            intents = self.blackboard.get_state_value("finalizer_amend_intents", {}) or {}
            intents.update(amend_intents)
            self.blackboard.update_state_value("finalizer_amend_intents", intents)
        self.blackboard.update_state_value("work_finalizer_result", applied)
        logger.info("[%s] finalized %d node(s)", self.name, len(applied))
        self.blackboard.update_state_value("last_agent", self.name)

    def _judge(self, wo, node, scope) -> dict:
        from app.assistant.dayflow_orchestrator.work_portfolio import (
            node_result, render_work_portfolio, STATUS_LEGEND,
        )
        projection = STATUS_LEGEND + "\n\n" + render_work_portfolio(wo)
        # The node's RESULT is the evidence it produced — `content` is its (immutable) directive.
        result_text = node_result(wo, node) or "(no result recorded)"
        info = (f"JUST-COMPLETED NODE — id: {node.id} | title: {node.title}\n"
                f"ITS FULL RESULT:\n{result_text}")
        agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_finalizer")
        res = agent.action_handler(Message(task=projection, information=info, scope_context=scope))
        return getattr(res, "data", {}) or {}

    def _apply(self, store, wo, node, data, verdict) -> dict:
        """Carry out the verdict on the graph. done->closed is the only path to the satisfied terminal."""
        from app.assistant.subconscious.concern_feedback import propagate_work_outcome
        if verdict == "resolve":
            disp = str(data.get("resolve_disposition") or "close").strip().lower()
            if disp == "abandon":
                store.apply("set_work_status", {"work_id": wo.id,
                                        "reason": f"finalizer resolve: {str(data.get('reasoning') or '')}",
                                        "status": "abandoned"}, actor="finalizer")
                propagate_work_outcome(store, wo.id, "abandoned")
                return {"note": "resolve -> WO abandoned"}
            store.apply("set_status", {"work_id": wo.id, "node_id": node.id, "status": "closed",
                                   "verdict": verdict,
                                   "reason": str(data.get("reasoning") or "finalizer accepted the result")},
                    actor="finalizer")
            store.apply("set_work_status", {"work_id": wo.id,
                                        "reason": f"finalizer resolve: {str(data.get('reasoning') or '')}",
                                        "status": "done"}, actor="finalizer")
            propagate_work_outcome(store, wo.id, "done")
            return {"note": "resolve -> WO done"}
        # proceed AND amend both close the node (its result is consumed); amend additionally re-plans.
        store.apply("set_status", {"work_id": wo.id, "node_id": node.id, "status": "closed",
                                   "verdict": verdict,
                                   "reason": str(data.get("reasoning") or "finalizer accepted the result")},
                    actor="finalizer")
        if verdict == "amend":
            return {"replan": True, "amend_intent": str(data.get("amend_intent") or ""), "note": "amend -> closed + replan"}
        return {"note": "proceed -> closed"}

    def _scope(self, message):
        scope = getattr(message, "scope_context", None)
        if scope is not None:
            return scope
        from app.assistant.scope.loader import load_scope_for_source
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=self.name)
