"""Post-LLM persist node for strategic_planner_wo (the dayflow evaluator — formerly the steward).

Applies the evaluator's output to the dayflow WorkObject store: mint new work objects from
`new_or_changed`, close `complete_work_ids` / `abandon_work_ids` (via the set_work_status op).
`replan_work_ids` is left on the blackboard for the architect (work_architect_node) to re-plan.

It also CONSUMES intake: for each work object CREATED this pass, the intake items the evaluator cited
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

        # replan_work_ids flows on to the architect (re-plan an existing work object's graph).
        # (advance is gone — work_execution runs every ready node; it never gated on it.)
        self.blackboard.update_state_value("replan_work_ids",
                                           self.blackboard.get_state_value("replan_work_ids", []) or [])
        self.blackboard.update_state_value("steward_persist_result", result)
        logger.info("[%s] persisted: created=%d completed=%d abandoned=%d",
                    self.name, len(result.get("created", [])), len(result.get("completed", [])),
                    len(result.get("abandoned", [])))
        self.blackboard.update_state_value("last_agent", self.name)

    def _close_consumed_items(self, created):
        """Consume + close the intake items cited in based_on of the work objects created this pass.

        Each cited artifact is CONSUMED: its summary is folded into the work object's goal (so the
        worker/architect see the originating intake, not just the one-line objective), then the item is
        closed — handed off to the work object so state_mover/action_selector don't also act on it. Only
        items present in THIS tick's admitted_artifacts are eligible (we never touch unrelated items);
        only created work objects consume (changed/advanced ones already did)."""
        if not created:
            return
        admitted = self.blackboard.get_state_value("admitted_artifacts", []) or []
        id_map = {}            # short_id / item_id -> real item_id
        summary_by_item = {}   # real item_id -> its summary text
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
            summary_by_item[item_id] = str(
                meta.get("summary") or it.get("summary") or meta.get("email_summary") or "").strip()
        if not id_map:
            return

        to_close = {}          # item_id -> work_id (for the close reason / log)
        consumed_by_work = {}  # work_id -> [summary, ...] to fold into its goal
        for c in created:
            if not isinstance(c, dict):
                continue
            wid = str(c.get("work_id") or "").strip()
            for ref in c.get("based_on", []) or []:
                real = id_map.get(str(ref).strip())
                if not real:
                    continue
                to_close.setdefault(real, wid)
                summ = summary_by_item.get(real, "")
                if summ and wid:
                    consumed_by_work.setdefault(wid, []).append(summ)
        if not to_close:
            return

        self._fold_intake_into_goals(consumed_by_work)

        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        for item_id, wid in to_close.items():
            try:
                write_dayflow_item(item_id, state="closed",
                                   reason=f"converted_to_work_object:{wid}", caller=self.name)
                logger.info("[%s] consumed intake item %s -> work object %s", self.name, item_id, wid)
            except Exception as e:
                logger.warning("[%s] could not close consumed item %s: %s", self.name, item_id, e)

    def _fold_intake_into_goals(self, consumed_by_work):
        """Append the consumed artifacts' summaries to each work object's goal content — a content-only
        update (set_status with the goal's CURRENT status) so the architect + worker see the originating
        intake. Per-work failures are logged and never abort the consume."""
        if not consumed_by_work:
            return
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
        for wid, summaries in consumed_by_work.items():
            uniq = [s for i, s in enumerate(summaries) if s and s not in summaries[:i]]
            if not uniq:
                continue
            try:
                wo = store.load(wid)
                goal = wo.nodes.get(wo.goal_node_id)
                if goal is None:
                    continue
                source = "\n\n— Originating intake —\n" + "\n".join(f"- {s}" for s in uniq)
                store.apply("set_status",
                            {"work_id": wid, "node_id": goal.id, "status": goal.status,
                             "content": (goal.content or "") + source}, actor="steward")
                logger.info("[%s] folded %d intake summary(ies) into goal of %s", self.name, len(uniq), wid)
            except Exception as e:
                logger.warning("[%s] could not fold intake into goal %s: %s", self.name, wid, e)
