"""
WorkObjectRenderNode — the pre-node that runs before every WorkerPlanner turn.

It loads the WorkObject node the planner owns and renders it (its node, its checklist
= child subtask nodes, recorded outputs, resolved dependencies, and the work tree)
into `work_projection` on the blackboard; the planner's user.j2 renders that. Re-runs
each cycle so the planner always sees the CURRENT node state (subtasks it just added,
statuses it just changed) — current-state + the inherited recent_history together.

The render is also exposed as the module function `render_work_projection(wo, node_id)`
so WorkPlanner can refresh the same view before a decision when resume routing skips
the pre-node. WorkFinalAnswer is a dormant class and is not an active finalizer.

work_objects is imported LAZILY so this node loads at boot even when work_objects is
absent; it only does real work inside a WorkObject run (where a WorkContext is set).
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def render_worker_provenance(wo, node_id: str) -> str:
    """Render full owned history using the shared Jinja execution-history view."""
    from app.assistant.dayflow_orchestrator.work_context import render_view, record_data
    return render_view("provenance", records=[record_data(n) for n in wo.provenance_for(node_id)])


def render_work_projection(wo, node_id: str) -> str:
    """Prepare typed worker context and render its Jinja view without clipping history."""
    from app.assistant.dayflow_orchestrator.work_context import render_view, worker_data
    return render_view("worker", view=worker_data(wo, node_id))


class WorkObjectRenderNode(ControlNode):
    def action_handler(self, message):
        try:
            projection = self._render()
        except Exception as e:
            logger.error("[%s] WorkObject render failed: %s", self.name, e, exc_info=True)
            self.blackboard.update_state_value("work_projection", "")
            raise
        self.blackboard.update_state_value("work_projection", projection)
        # Route on via state_map: clear the stale next_agent (else the delegator re-runs
        # this node), and set last_agent so it picks state_map[self.name] = the planner.
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)

    # --------------------------------------------------------------------- #
    def _render(self) -> str:
        from work_objects.runtime import get_work_context
        ctx = get_work_context()
        wo = ctx.store.load(ctx.work_id)
        return render_work_projection(wo, ctx.node_id)
