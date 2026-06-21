"""Execution node for the work-object dayflow pipeline (Phase 2).

Runs AFTER strategic_planner_wo_persist_node. Consumes the steward's freshly `created` work
objects + its `advance_work_ids`, and drives each one forward with the standalone work_on arm:
`work_on(store, work_id, node_id=goal_id)` makes work_emi_team own the goal, decompose its checklist
into subtask nodes, work them, and finish (or resume, on a later pass). Cold-start and resume are the
same call — the render worker re-loads the node each time.

Bounded: at most _MAX_EXECUTIONS_PER_TICK work objects per tick (the steward gates which advance; this
is a backstop so one tick can't run forever). Never raises — a work-object failure degrades to "no
execution this tick" and the pipeline continues, rather than crashing the live dayflow loop.

Inert until the dayflow manager's state_map routes to it (Phase 2 cutover).
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MAX_EXECUTIONS_PER_TICK = 3
_TERMINAL_WO_STATES = {"done", "abandoned"}


class WorkExecutionNode(ControlNode):
    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        executed = []
        try:
            from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
            from work_objects.work_runtime import work_on
            store = get_dayflow_work_store()

            persist = self.blackboard.get_state_value("steward_persist_result", {}) or {}
            created = [c.get("work_id") for c in (persist.get("created") or [])
                       if isinstance(c, dict) and c.get("work_id")]
            advance = [w for w in (self.blackboard.get_state_value("advance_work_ids", []) or []) if w]
            # created first (cold-start the new goals), then steward-flagged advances; dedup, ordered.
            ordered = list(dict.fromkeys([*created, *advance]))

            runnable = []
            for work_id in ordered:
                try:
                    wo = store.load(work_id)
                except Exception as e:
                    logger.warning("[%s] work object %s not loadable: %s", self.name, work_id, e)
                    continue
                if str(wo.status or "").lower() in _TERMINAL_WO_STATES:
                    continue
                runnable.append((work_id, wo.goal_node_id))

            deferred = runnable[_MAX_EXECUTIONS_PER_TICK:]
            for work_id, goal_id in runnable[:_MAX_EXECUTIONS_PER_TICK]:
                try:
                    status = work_on(store, work_id, node_id=goal_id)
                    executed.append({"work_id": work_id, "status": status})
                    logger.info("[%s] advanced work object %s -> %s", self.name, work_id, status)
                except Exception as e:
                    logger.error("[%s] work_on failed for %s: %s", self.name, work_id, e)
                    logger.debug("[%s] work_on exception", self.name, exc_info=True)
                    executed.append({"work_id": work_id, "status": "error"})

            if deferred:
                logger.info("[%s] deferred %d work object(s) to a later tick: %s",
                            self.name, len(deferred), [w for w, _ in deferred])
        except Exception as e:
            logger.error("[%s] execution node failed: %s", self.name, e)
            logger.debug("[%s] execution node exception", self.name, exc_info=True)

        self.blackboard.update_state_value("work_execution_result", executed)
        logger.info("[%s] executed %d work object(s)", self.name, len(executed))
        self.blackboard.update_state_value("last_agent", self.name)
