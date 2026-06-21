"""Post-LLM persist node for strategic_planner_wo (the work-object steward).

Applies the steward's output to the dayflow WorkObject store: mint new work objects from
`new_or_changed`, close `complete_work_ids` / `abandon_work_ids` (via the set_work_status op).
`advance_work_ids` is left on the blackboard for the execution phase (Phase 2: bounded work_on).
The work-object analogue of planner_persist_node.

Inert until the dayflow manager's state_map routes to it (Phase 1 cutover).
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class StrategicPlannerWoPersistNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        output = {
            "new_or_changed": self.blackboard.get_state_value("new_or_changed", []) or [],
            "complete_work_ids": self.blackboard.get_state_value("complete_work_ids", []) or [],
            "abandon_work_ids": self.blackboard.get_state_value("abandon_work_ids", []) or [],
        }
        result = {"created": [], "changed": [], "completed": [], "abandoned": []}
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from app.assistant.dayflow_orchestrator.work_persist import persist_steward_output
            store = get_dayflow_work_store()
            result = persist_steward_output(store, output)
        except Exception as e:
            logger.error("[%s] persist failed: %s", self.name, e)
            logger.debug("[%s] persist exception", self.name, exc_info=True)

        # advance_work_ids flows on to the execution phase (Phase 2).
        self.blackboard.update_state_value("advance_work_ids",
                                           self.blackboard.get_state_value("advance_work_ids", []) or [])
        self.blackboard.update_state_value("steward_persist_result", result)
        logger.info("[%s] persisted: created=%d completed=%d abandoned=%d",
                    self.name, len(result.get("created", [])), len(result.get("completed", [])),
                    len(result.get("abandoned", [])))
        self.blackboard.update_state_value("last_agent", self.name)
