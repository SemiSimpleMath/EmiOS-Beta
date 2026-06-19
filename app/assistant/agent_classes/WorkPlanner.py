"""
WorkPlanner — the WORKER version of Planner.

It owns exactly ONE WorkObject node and advances it like any planner: one action
per turn, recent_history, critic — all inherited from Planner/Agent unchanged.

The ONLY behavioral diff is `_reconcile_to_graph`: after each decision it mirrors the
declared `checklist` -> child subtask nodes (stable id, immutable name, status, and a
closing `evidence` note on the node), declaratively (NO tool calls). `findings_to_pod`
is still minted to durable pods by the inherited `Planner._mint_research_findings`; the
final-answer agent surfaces that pod and the dispatch layer attaches it to the task node.
So the `checklist` round-trips through the durable graph instead of the blackboard, and
the single `action` channel stays free for real work.

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
        by_id = {n.id: n for n in wo.nodes.values()
                 if n.parent_id == node_id and n.type == "subtask"}

        # checklist -> subtask nodes, mirrored every turn. Items are ChecklistItem dicts: a stable
        # `id` (echoed back via the render node), an IMMUTABLE `name`, a `status`, and — on close — an
        # `evidence` note. Match by id and flip status (writing the evidence note ONTO the node on
        # done/abandoned); an empty id adds a new checkpoint. Identity is the id, so a re-worded name
        # can't fork a node — nothing to dedup.
        for item in (result_dict.get("checklist") or []):
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            name = str(item.get("name") or item.get("text") or "").strip()
            status = str(item.get("status") or "todo").strip().lower()
            evidence = str(item.get("evidence") or "").strip()
            if not name:
                continue
            if cid and cid in by_id:
                self._transition_subtask(store, work_id, cid, by_id[cid].status, status, actor, evidence)
            elif not cid:
                tools.add_subtask(name)

        # findings_to_pod is handled by the base Planner._mint_research_findings (it mints the durable
        # research pod + research_notebook); the final-answer agent surfaces it as pod_references and
        # the dispatch layer (run_node) attaches that pod to the task node. We do NOT mirror findings
        # as graph nodes, and `progress` mints nothing.

    @staticmethod
    def _transition_subtask(store, work_id, node_id, cur, target, actor, evidence: str = "") -> None:
        """Respect the spine state machine (proposed->active->done). A checklist item the owner did
        itself was never separately dispatched, so it sits 'proposed'; step it through 'active' to
        reach 'done'. On the closing transition, write the `evidence` note onto the node's content."""
        def _set(status, content=None):
            data = {"work_id": work_id, "node_id": node_id, "status": status}
            if content is not None:
                data["content"] = content
            store.apply("set_status", data, actor=actor)
        try:
            if target == "done" and cur != "done":
                if cur == "proposed":
                    _set("active"); cur = "active"
                if cur in {"active", "waiting"}:
                    _set("done", content=evidence or None)
            elif target == "active" and cur == "proposed":
                _set("active")
            elif target == "abandoned" and cur in {"proposed", "active", "waiting", "failed"}:
                _set("abandoned", content=evidence or None)
        except Exception as e:
            logger.debug("subtask transition %s %s->%s skipped: %s", node_id, cur, target, e)
