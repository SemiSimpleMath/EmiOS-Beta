"""
WorkPlanner — the WORKER version of Planner.

It owns exactly ONE WorkObject node and advances it like any planner: one action
per turn, recent_history, critic — all inherited from Planner/Agent unchanged.

The ONLY behavioral diff is `_reconcile_to_graph`: after each decision it mirrors
the declared `checklist` -> child subtask nodes and `progress` -> Evidence nodes,
declaratively (NO tool calls), exactly the way the base `Planner._mint_research_findings`
mirrors `findings_to_pod` -> pods. So the planner's `checklist` round-trips through
the durable graph instead of the blackboard, and the single `action` channel stays
free for real work.

work_objects is imported LAZILY inside the hook so this class loads cleanly at boot
(agent-registry scan) even when work_objects is absent; the hook is a no-op unless a
WorkContext is active (i.e. this planner is actually driving a WorkObject node).
"""
from app.assistant.agent_classes.Planner import Planner
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class WorkPlanner(Planner):
    def process_llm_result(self, result):
        super().process_llm_result(result)
        try:
            self._reconcile_to_graph(result)
        except Exception as e:
            logger.error("[%s] WorkObject reconcile failed: %s", self.name, e)
            logger.debug("[%s] reconcile exception", self.name, exc_info=True)

    # --------------------------------------------------------------------- #
    def _reconcile_to_graph(self, result_dict) -> None:
        if not isinstance(result_dict, dict):
            return
        try:
            from work_objects.runtime import get_work_context
            from work_objects.tools import WorkGraphTools
        except Exception:
            return  # work_objects not on path -> not a WorkObject run
        try:
            ctx = get_work_context()
        except RuntimeError:
            return  # this planner is not driving a WorkObject node -> no-op

        store, work_id, node_id, actor = ctx.store, ctx.work_id, ctx.node_id, ctx.actor
        tools = WorkGraphTools(store, work_id, node_id, actor)
        wo = store.load(work_id)
        children = {n.id: n for n in wo.nodes.values()
                    if n.parent_id == node_id and n.type == "subtask"}

        # 1) checklist -> subtask nodes (new -> add; done/abandoned -> close)
        for item in (result_dict.get("checklist") or []):
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            status = str(item.get("status") or "todo").strip().lower()
            if not text:
                continue
            if cid and cid in children:
                self._transition_subtask(store, work_id, cid, children[cid].status, status, actor)
            elif not cid:
                tools.add_subtask(text)        # new subtask the planner just added

        # 2) progress -> Evidence nodes (the curator's cross-agent currency)
        for p in (result_dict.get("progress") or []):
            claim = str(p).strip()
            if claim:
                tools.record_finding(claim)

    @staticmethod
    def _transition_subtask(store, work_id, node_id, cur, target, actor) -> None:
        """Respect the spine state machine (proposed->active->done). A checklist item
        the owner did itself was never separately dispatched, so it sits 'proposed';
        step it through 'active' to reach 'done'."""
        def _set(status):
            store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": status}, actor=actor)
        try:
            if target == "done" and cur != "done":
                if cur == "proposed":
                    _set("active"); cur = "active"
                if cur in {"active", "waiting"}:
                    _set("done")
            elif target == "abandoned" and cur in {"proposed", "active", "waiting", "failed"}:
                _set("abandoned")
        except Exception as e:
            logger.debug("subtask transition %s %s->%s skipped: %s", node_id, cur, target, e)
