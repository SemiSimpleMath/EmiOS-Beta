"""Decompose freshly-created goals into their DAG (Part 2 of the split planner).

Runs right after strategic_planner_wo_persist_node. For each work object the steward CREATED this tick,
invoke dayflow_orchestrator::work_architect on its objective and lay the resulting DAG (subtask nodes,
depends_on edges, wait-gates) into the graph via apply_architect_dag. work_execution_node then runs the
ready nodes. Changed/advanced goals are NOT re-decomposed here (that reconcile is #54). Never raises —
a decompose failure leaves the goal as a bare goal node and the pipeline continues.

Inert until the dayflow manager's state_map routes to it.
"""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


class WorkArchitectNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        decomposed = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
            store = get_dayflow_work_store()
            persist = self.blackboard.get_state_value("steward_persist_result", {}) or {}
            created = [c for c in (persist.get("created") or [])
                       if isinstance(c, dict) and c.get("work_id") and c.get("objective")]
            if created:
                scope = self._scope(message)
                agent = DI.agent_factory.create_agent("dayflow_orchestrator::work_architect")
                for c in created:
                    work_id = c["work_id"]
                    try:
                        result = agent.action_handler(Message(task=c["objective"], scope_context=scope))
                        nodes = (getattr(result, "data", {}) or {}).get("nodes", []) or []
                        res = apply_architect_dag(store, work_id, nodes)
                        decomposed.append({"work_id": work_id, "nodes": len(res.get("added", []))})
                        logger.info("[%s] decomposed %s into %d node(s)",
                                    self.name, work_id, len(res.get("added", [])))
                    except Exception as e:
                        logger.error("[%s] decompose failed for %s: %s", self.name, work_id, e)
                        logger.debug("[%s] decompose exception", self.name, exc_info=True)
        except Exception as e:
            logger.error("[%s] architect node failed: %s", self.name, e)
            logger.debug("[%s] architect node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_decompose_result", decomposed)
        self.blackboard.update_state_value("last_agent", self.name)

    def _scope(self, message):
        scope = getattr(message, "scope_context", None)
        if scope is not None:
            return scope
        from app.assistant.scope.loader import load_scope_for_source
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=self.name)
