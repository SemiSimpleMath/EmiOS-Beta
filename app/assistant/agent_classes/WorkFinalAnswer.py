"""
WorkFinalAnswer — the finalizer for a work-graph node.

It runs once at node termination (the planner returned control, or a graceful exit), synthesizes
the node's outcome, and — this is its job — MINTS THE OWNED NODE'S TERMINAL STATUS from its
`node_status` verdict: complete -> done, abandoned -> abandoned, error -> failed, writing
`result_summary` onto the node as its closing note.

This mirrors WorkPlanner._reconcile_to_graph: the planner closes the node's child subtasks; the
finalizer closes the one node the manager owns. The dispatch layer (run_node) keeps a guarded
backstop only for the case where this hook never ran (a hard abort, or the web stack).

work_objects is imported LAZILY inside the hook so the class loads cleanly at boot (agent-registry
scan) even when work_objects is absent; the hook is a no-op unless a WorkContext is active.
"""
from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# verdict (LLM) -> spine terminal status (graph)
_VERDICT_TO_STATUS = {
    "complete": "done", "done": "done",
    "abandoned": "abandoned", "abandon": "abandoned",
    "error": "failed", "failed": "failed",
}


class WorkFinalAnswer(Agent):
    def action_handler(self, message):
        # Re-render the owned node FRESH from the graph right before our turn, so we synthesize from
        # the planner's FINAL findings write. The standard render node ran one turn earlier (before
        # the planner's last turn), so its work_projection is stale by exactly that write.
        self._refresh_work_projection()
        return super().action_handler(message)

    def process_llm_result(self, result):
        super().process_llm_result(result)
        try:
            self._write_node_termination(result)
        except Exception as e:
            logger.error("[%s] WorkObject finalize failed: %s", self.name, e)
            logger.debug("[%s] finalize exception", self.name, exc_info=True)

    # --------------------------------------------------------------------- #
    def _refresh_work_projection(self) -> None:
        try:
            from work_objects.runtime import get_work_context
            from app.assistant.control_nodes.workobject_render_node import render_work_projection
        except Exception:
            return  # work_objects not on path -> not a WorkObject run
        try:
            ctx = get_work_context()
        except RuntimeError:
            return  # not finalizing a WorkObject node -> no-op
        try:
            wo = ctx.store.load(ctx.work_id)
            self.blackboard.update_state_value("work_projection", render_work_projection(wo, ctx.node_id))
        except Exception as e:
            logger.debug("[%s] work_projection refresh skipped: %s", self.name, e)

    # --------------------------------------------------------------------- #
    def _write_node_termination(self, result_dict) -> None:
        if not isinstance(result_dict, dict):
            return
        try:
            from work_objects.runtime import get_work_context
        except Exception:
            return  # work_objects not on path -> not a WorkObject run
        try:
            ctx = get_work_context()
        except RuntimeError:
            return  # this agent is not finalizing a WorkObject node -> no-op

        verdict = str(result_dict.get("node_status") or "").strip().lower()
        target = _VERDICT_TO_STATUS.get(verdict)
        if target is None:
            # No clear verdict — leave the close to run_node's backstop rather than guess.
            logger.debug("[%s] no usable node_status verdict (%r); deferring to backstop",
                         self.name, verdict)
            return

        store, work_id, node_id = ctx.store, ctx.work_id, ctx.node_id
        node = store.load(work_id).nodes.get(node_id)
        if node is None or node.is_terminal:
            return
        data = {"work_id": work_id, "node_id": node_id, "status": target}
        note = str(result_dict.get("result_summary") or "").strip()
        if note:
            data["content"] = note
        store.apply("set_status", data, actor="worker_agents::final_answer")
        logger.info("[%s] node %s -> %s (verdict=%s)", self.name, node_id, target, verdict)
