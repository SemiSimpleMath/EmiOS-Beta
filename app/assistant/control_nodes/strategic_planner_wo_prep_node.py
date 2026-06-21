"""Pre-LLM prep node for strategic_planner_wo (the work-object steward).

Builds the steward's context from the dayflow WorkObject store: renders the ACTIVE work objects as the
`work_portfolio` the planner reasons over. Intake (admitted_artifacts) + recent outcomes are already on
the blackboard from earlier nodes. The work-object analogue of strategic_planner_prep_node.

Inert until the dayflow manager's state_map routes to it (Phase 1 cutover).
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TERMINAL_WO_STATES = {"done", "abandoned"}


class StrategicPlannerWoPrepNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        portfolio = "(no active work objects)"
        n_active = 0
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_portfolio import render_portfolio
            store = get_dayflow_work_store()
            active = [store.load(s["id"]) for s in store.list_work_objects()
                      if str(s.get("status") or "").lower() not in _TERMINAL_WO_STATES]
            n_active = len(active)
            portfolio = render_portfolio(active)
        except Exception as e:
            logger.error("[%s] portfolio build failed: %s", self.name, e)
            logger.debug("[%s] portfolio build exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_portfolio", portfolio)
        logger.info("[%s] prepared: %d active work object(s)", self.name, n_active)
        self.blackboard.update_state_value("last_agent", self.name)
