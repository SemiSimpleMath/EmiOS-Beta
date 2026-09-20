"""Post-LLM persist node for strategic_planner_wo (the dayflow evaluator — formerly the steward).

Applies the evaluator's output to the dayflow WorkObject store: mint new work objects from
`new_or_changed`, close `complete_work_ids` / `abandon_work_ids` (via the set_work_status op).
`replan_work_ids` is left on the blackboard for the architect (work_architect_node) to re-plan.

It also CONSUMES intake: for each work object created or changed this pass, the intake items the evaluator cited
in its `based_on` have their content folded into the work object's goal (so the worker sees the
originating intake, not just the one-line objective) and are then closed (state -> closed, reason
"converted_to_work_object") so nothing else acts on them. Intake the evaluator did not convert stays
open as context. The work-object analogue of planner_persist_node.

Inert until the dayflow manager's state_map routes to it.
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
            result = persist_steward_output(store, output,
                admitted_artifacts=self.blackboard.get_state_value("admitted_artifacts", []) or [])
        except Exception as e:
            logger.error("[%s] persist failed: %s", self.name, e)
            logger.debug("[%s] persist exception", self.name, exc_info=True)
            raise

        # Hand consumed intake off to the work objects: close the items cited in based_on of the
        # CREATED work objects, so the direct path doesn't double-handle them.
        try:
            self._close_consumed_items([*(result.get("created") or []), *(result.get("changed_records") or [])])
        except Exception as e:
            logger.error("[%s] consumed-item close failed: %s", self.name, e)
            logger.debug("[%s] consumed-item close exception", self.name, exc_info=True)
            raise

        # replan_work_ids flows on to the architect (re-plan an existing work object's graph).
        # (advance is gone — work_execution runs every ready node; it never gated on it.)
        self.blackboard.update_state_value("replan_work_ids",
                                           self.blackboard.get_state_value("replan_work_ids", []) or [])
        self.blackboard.update_state_value("steward_persist_result", result)
        logger.info("[%s] persisted: created=%d completed=%d abandoned=%d",
                    self.name, len(result.get("created", [])), len(result.get("completed", [])),
                    len(result.get("abandoned", [])))
        self.blackboard.update_state_value("last_agent", self.name)

    def _close_consumed_items(self, transferred):
        """Close cited intake only after its source details are durable on the goal.

        Creation already carries the source in its first commit. The revision here
        supports explicit handoff to an existing object and preserves prior sources.
        A failed graph or item write raises, leaving the durable inbox retryable.
        """
        if not transferred:
            return
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        from app.assistant.dayflow_orchestrator.work_intake import source_records, goal_update
        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        store = get_dayflow_work_store()
        admitted = self.blackboard.get_state_value("admitted_artifacts", []) or []
        for record in transferred:
            wid = record["work_id"]
            sources = source_records(admitted, record.get("based_on") or [])
            if not sources:
                continue
            wo = store.load(wid)
            existing = (getattr(wo, "constraints", {}) or {}).get("source_intake") or []
            if any(source not in existing for source in sources):
                store.apply("revise_goal", goal_update(wo, sources=sources), actor="steward")
            for source in sources:
                write_dayflow_item(source["item_id"], state="closed",
                                   reason=f"converted_to_work_object:{wid}", caller=self.name)
