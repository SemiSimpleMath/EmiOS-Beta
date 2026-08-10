"""Control node: the WORK REPAIR pass — adjudicate work objects stuck on a FAILED node.

Runs after the architect in the work-object pipeline. For each ACTIVE work object that has a failed node, invoke the work_repair
adjudicator on its projection and apply the disposition (escalate / retry / abandon_goal) via
apply_adjudication: escalate re-issues the failed step as a user_reply ask (the dispatch then asks the
user and the reply is recorded as the node's result), retry re-opens it to re-run, abandon_goal drops the WO.
This drains the stuck pile and resolves new failures instead of leaving them 'active'. Bounded per tick;
never raises.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_MAX_REPAIRS_PER_TICK = 3
_TERMINAL_WO_STATES = {"done", "abandoned"}


class WorkRepairNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        repaired = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio, STATUS_LEGEND
            from app.assistant.dayflow_orchestrator.work_repair_apply import apply_adjudication
            store = get_dayflow_work_store()

            stuck = []
            for s in store.list_work_objects():
                if str(s.get("status") or "").lower() in _TERMINAL_WO_STATES:
                    continue
                try:
                    wo = store.load(s["id"])
                except Exception:
                    continue
                if any(n.status == "failed" for n in wo.nodes.values()):
                    stuck.append(wo)

            if stuck:
                scope = self._scope(message)
                for wo in stuck[:_MAX_REPAIRS_PER_TICK]:
                    try:
                        # User replies live as evidence children of the ask node and render in the
                        # projection's WHY/RESULT lines — the adjudicator reads declines from there.
                        projection = STATUS_LEGEND + "\n\n" + render_work_portfolio(wo)
                        agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_repair")
                        result = agent.action_handler(Message(task=projection, scope_context=scope))
                        data = getattr(result, "data", {}) or {}
                        disp = str(data.get("disposition") or "").strip().lower()
                        ask = str(data.get("ask") or "")
                        if not disp:
                            continue
                        res = apply_adjudication(store, wo.id, disp, ask=ask,
                                                 reason=str(data.get("reasoning") or "").strip())
                        repaired.append(res)
                        logger.info("[%s] %s -> %s (targets=%s)", self.name, wo.id, disp, res.get("targets"))
                    except Exception as e:
                        logger.error("[%s] repair failed for %s: %s", self.name, wo.id, e)
                        logger.debug("[%s] repair exception", self.name, exc_info=True)
        except Exception as e:
            logger.error("[%s] work_repair node failed: %s", self.name, e)
            logger.debug("[%s] work_repair node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_repair_result", repaired)
        logger.info("[%s] repaired %d work object(s)", self.name, len(repaired))
        self.blackboard.update_state_value("last_agent", self.name)

    def _scope(self, message):
        scope = getattr(message, "scope_context", None)
        if scope is not None:
            return scope
        from app.assistant.scope.loader import load_scope_for_source
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=self.name)
