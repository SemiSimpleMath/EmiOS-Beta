"""Control node: the WORK FINALIZER pass (SHADOW) — reconcile each freshly-completed node's result into
its whole work object.

Runs after work_repair_node. For each recently-active work object with a recently-`done` node, it invokes
the work_finalizer adjudicator on {the WO projection + that node's FULL, un-truncated result} and the
adjudicator returns a verdict: PROCEED (the plan holds) / AMEND (the result changes the plan) / RESOLVE
(the goal is settled).

SHADOW MODE: this node ONLY logs the verdict + records it on the blackboard. It does NOT close nodes, flag
re-plans, or close work objects — the existing rollup still owns closing. This lets the finalizer's
judgment be validated against real ticks before it is made authoritative (stage 3 flips closing onto it
and routes AMEND to the architect). Bounded per tick; never raises.

Inert until the dayflow manager's state_map routes to it.
"""
from datetime import timedelta

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import parse_iso_utc

logger = get_logger(__name__)

_MAX_FINALIZE_PER_TICK = 5
_LOOKBACK_MINUTES = 8     # judge a node done within ~the last couple of ticks (shadow: bounded re-judge, no marker)
_BODY_CHARS = 4000        # the node's FULL result — un-truncated vs render_work_portfolio's 400-char body


class WorkFinalizerNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        verdicts = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio, STATUS_LEGEND
            from work_objects.model import utcnow
            store = get_dayflow_work_store()
            now = utcnow()
            cutoff = now - timedelta(minutes=_LOOKBACK_MINUTES)

            # Recently-active work objects (terminal OR not — we WANT to see a goal-completing node even if
            # the rollup just closed its WO), each with a node that went `done` in the lookback window.
            candidates = []   # (wo, node)
            for s in store.list_work_objects():   # newest-updated first
                upd_wo = parse_iso_utc(str(s.get("updated_at") or ""))
                if upd_wo is not None and upd_wo < cutoff:
                    break   # everything after is older than the window
                try:
                    wo = store.load(s["id"])
                except Exception:
                    continue
                gid = wo.goal_node_id
                for n in wo.nodes.values():
                    if n.id == gid or n.status != "done":
                        continue
                    upd = getattr(n, "updated_at", None)
                    if upd is not None and upd >= cutoff:
                        candidates.append((wo, n))

            if candidates:
                scope = self._scope(message)
                for wo, node in candidates[:_MAX_FINALIZE_PER_TICK]:
                    try:
                        projection = STATUS_LEGEND + "\n\n" + render_work_portfolio(wo)
                        result_text = (node.content or "").strip() or "(no result text recorded)"
                        info = (f"JUST-COMPLETED NODE — id: {node.id} | title: {node.title}\n"
                                f"ITS FULL RESULT:\n{result_text[:_BODY_CHARS]}")
                        agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_finalizer")
                        res = agent.action_handler(Message(task=projection, information=info, scope_context=scope))
                        data = getattr(res, "data", {}) or {}
                        verdict = str(data.get("verdict") or "").strip().lower()
                        if not verdict:
                            continue
                        verdicts.append({
                            "work_id": wo.id, "node_id": node.id, "verdict": verdict,
                            "amend_intent": str(data.get("amend_intent") or ""),
                            "resolve_disposition": str(data.get("resolve_disposition") or ""),
                        })
                        logger.info("[work_finalizer SHADOW] %s::%s -> %s | %s", wo.id, node.id, verdict,
                                    (data.get("reasoning") or "")[:140])
                    except Exception as e:
                        logger.error("[%s] finalize failed for %s::%s: %s", self.name, wo.id, node.id, e)
                        logger.debug("[%s] finalize exception", self.name, exc_info=True)
        except Exception as e:
            logger.error("[%s] work_finalizer node failed: %s", self.name, e)
            logger.debug("[%s] work_finalizer node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_finalizer_shadow_result", verdicts)
        logger.info("[%s] SHADOW judged %d completed node(s)", self.name, len(verdicts))
        self.blackboard.update_state_value("last_agent", self.name)

    def _scope(self, message):
        scope = getattr(message, "scope_context", None)
        if scope is not None:
            return scope
        from app.assistant.scope.loader import load_scope_for_source
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=self.name)
