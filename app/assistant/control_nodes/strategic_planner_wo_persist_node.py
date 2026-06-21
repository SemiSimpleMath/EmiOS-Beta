"""Post-LLM persist node for strategic_planner_wo (the work-object steward).

Applies the steward's output to the dayflow WorkObject store: mint new work objects from
`new_or_changed`, close `complete_work_ids` / `abandon_work_ids` (via the set_work_status op).
`advance_work_ids` is left on the blackboard for the execution phase (work_execution_node).

It also HANDS OFF consumed intake: for each work object CREATED this pass, the intake items the
steward cited in its `based_on` are closed (state -> closed, reason "converted_to_work_object") so the
direct path (state_mover -> action_selector) does NOT also act on them. One-shot intake the steward
left alone stays open for the direct path. The work-object analogue of planner_persist_node.

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
            result = persist_steward_output(store, output)
        except Exception as e:
            logger.error("[%s] persist failed: %s", self.name, e)
            logger.debug("[%s] persist exception", self.name, exc_info=True)

        # Hand consumed intake off to the work objects: close the items cited in based_on of the
        # CREATED work objects, so the direct path doesn't double-handle them.
        try:
            self._close_consumed_items(result.get("created", []) or [])
        except Exception as e:
            logger.error("[%s] consumed-item close failed: %s", self.name, e)
            logger.debug("[%s] consumed-item close exception", self.name, exc_info=True)

        # advance_work_ids flows on to the execution phase (work_execution_node).
        self.blackboard.update_state_value("advance_work_ids",
                                           self.blackboard.get_state_value("advance_work_ids", []) or [])
        self.blackboard.update_state_value("steward_persist_result", result)
        logger.info("[%s] persisted: created=%d completed=%d abandoned=%d",
                    self.name, len(result.get("created", [])), len(result.get("completed", [])),
                    len(result.get("abandoned", [])))
        self.blackboard.update_state_value("last_agent", self.name)

    def _close_consumed_items(self, created):
        """Close the intake items cited in based_on of the work objects created this pass.

        The steward sees each intake item with its short id and cites it in based_on; closing those
        items hands them off to the work object so state_mover/action_selector don't also act on them.
        Only items present in THIS tick's admitted_artifacts are eligible (we never close unrelated
        items); only created work objects consume (changed/advanced ones already did)."""
        if not created:
            return
        admitted = self.blackboard.get_state_value("admitted_artifacts", []) or []
        id_map = {}  # short_id / item_id  ->  real item_id
        for it in admitted:
            if not isinstance(it, dict):
                continue
            meta = it.get("metadata", {}) or {}
            item_id = str(meta.get("item_id") or it.get("id") or "").strip()
            if not item_id:
                continue
            id_map[item_id] = item_id
            short_id = str(meta.get("short_id") or "").strip()
            if short_id:
                id_map[short_id] = item_id
        if not id_map:
            return

        to_close = {}  # item_id -> work_id (for the close reason / log)
        for c in created:
            if not isinstance(c, dict):
                continue
            wid = str(c.get("work_id") or "").strip()
            for ref in c.get("based_on", []) or []:
                real = id_map.get(str(ref).strip())
                if real:
                    to_close.setdefault(real, wid)
        if not to_close:
            return

        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        for item_id, wid in to_close.items():
            try:
                write_dayflow_item(item_id, state="closed",
                                   reason=f"converted_to_work_object:{wid}", caller=self.name)
                logger.info("[%s] closed intake item %s -> work object %s", self.name, item_id, wid)
            except Exception as e:
                logger.warning("[%s] could not close consumed item %s: %s", self.name, item_id, e)
