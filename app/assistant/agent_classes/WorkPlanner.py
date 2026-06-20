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
            from work_objects.runtime import active_attribution_node, get_work_context
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

        # checklist -> subtask nodes, mirrored every turn — UNIFORM for every WorkPlanner (coordinator
        # or worker; the work_* managers are one stack). Items carry a stable `id` (echoed via the render
        # node), an IMMUTABLE `name`, a `status`, and an `evidence` note on close. The single item the
        # planner marks in-progress becomes the ACTIVE node: this turn's findings and any manager it
        # delegates attribute to THAT node (tool results -> its evidence; a delegated manager runs as a
        # child UNDER it), not the parent the planner owns. Identity is the id, so a re-worded name can't
        # fork a node.
        def _spine(planner_status) -> str:
            s = str(planner_status or "todo").strip().lower()
            if s in ("in_progress", "in progress", "doing", "active"):
                return "active"
            if s in ("done", "complete", "completed"):
                return "done"
            if s in ("abandoned", "abandon", "dropped"):
                return "abandoned"
            if s in ("incomplete", "paused", "pause", "waiting", "on_hold", "on hold", "parked", "blocked"):
                return "waiting"   # set aside, re-activatable (the planner resumes it by marking in_progress again)
            return "todo"
        for item in (result_dict.get("checklist") or []):
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            name = str(item.get("name") or item.get("text") or "").strip()
            target = _spine(item.get("status"))
            evidence = str(item.get("evidence") or "").strip()
            if not name:
                continue
            if cid and cid in by_id:
                self._transition_subtask(store, work_id, cid, by_id[cid].status, target, actor, evidence)
            elif not cid:
                fresh = tools.add_subtask(name)
                if target in ("active", "waiting"):   # a brand-new item declared in_progress (or paused) this turn
                    self._transition_subtask(store, work_id, fresh, "proposed", target, actor, "")

        # The node this turn's work attributes to: the single in-progress checklist item (set above),
        # else the owned node (a single-step node with no checklist). findings -> evidence UNDER it (the
        # node's durable result). Deduped by content. process_llm_result runs EVERY turn, including the
        # return_control turn, so the planner's final write is always captured.
        target_node = active_attribution_node(store, work_id, node_id)
        from work_objects.model import new_id as _new_finding_id
        wo = store.load(work_id)
        recorded = {(n.content or "").strip() for n in wo.nodes.values()
                    if n.parent_id == target_node and n.type == "evidence"}
        for finding in (result_dict.get("findings") or []):
            finding = str(finding or "").strip()
            if finding and finding not in recorded:
                recorded.add(finding)
                store.apply("add_node",
                            {"work_id": work_id, "id": _new_finding_id("finding"), "type": "evidence",
                             "parent_id": target_node, "content": finding, "status": "assumed",
                             "created_by": actor},
                            actor=actor)

        # info_for_others -> shared fact nodes (root evidence) that `informs` the GOAL, so each
        # discovery reaches every other agent (render RELEVANT INFO) and the curator (which harvests
        # goal-informing facts). Deduped by content.
        for note in (result_dict.get("info_for_others") or []):
            note = str(note or "").strip()
            if note:
                self._attach_shared_fact(store, work_id, note, actor)

        # findings_to_pod is handled by the base Planner._mint_research_findings (it mints the durable
        # research pod + research_notebook); the final-answer agent surfaces it as pod_references and
        # the dispatch layer (run_node) attaches that pod to the task node. We do NOT mirror findings
        # as graph nodes, and `progress` mints nothing.

    @staticmethod
    def _transition_subtask(store, work_id, node_id, cur, target, actor, evidence: str = "") -> None:
        """Drive a checklist item along the spine (proposed -> active -> done|abandoned), with PAUSE:
        active <-> waiting. The planner marks an item in_progress (-> active); to set it aside to work
        another first it marks it paused/INCOMPLETE (-> waiting, re-activatable), and resumes by marking
        it in_progress again (waiting -> active). Exactly one item is 'active' at a time, which is what
        the attribution resolver keys on. On a closing transition the `evidence` note is written onto the
        node's content."""
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
            elif target == "active" and cur in {"proposed", "waiting"}:   # start a new item OR RESUME a paused one
                _set("active")
            elif target == "waiting" and cur in {"proposed", "active"}:   # PAUSE / set aside — re-activatable later
                _set("waiting", content=evidence or None)
            elif target == "abandoned" and cur in {"proposed", "active", "waiting", "failed"}:
                _set("abandoned", content=evidence or None)
        except Exception as e:
            logger.debug("subtask transition %s %s->%s skipped: %s", node_id, cur, target, e)

    @staticmethod
    def _attach_shared_fact(store, work_id, content: str, actor: str) -> None:
        """Mint a root `evidence` fact node (info useful to OTHER agents) + an `informs` edge to
        the GOAL — so it reaches every agent (render RELEVANT INFO) and the curator. Deduped by
        content against facts already informing the goal."""
        from work_objects.model import new_id
        try:
            wo = store.load(work_id)
            goal_id = wo.goal_node_id
            if not goal_id:
                return
            for e in wo.edges:
                if e.relation == "informs" and e.dst == goal_id:
                    n = wo.nodes.get(e.src)
                    if n is not None and (n.content or "").strip() == content:
                        return
            fid = new_id("fact")
            store.apply("add_node",
                        {"work_id": work_id, "id": fid, "type": "evidence", "content": content,
                         "status": "assumed", "created_by": actor},
                        actor=actor)
            store.apply("add_edge",
                        {"work_id": work_id, "src": fid, "dst": goal_id, "relation": "informs"},
                        actor=actor)
        except Exception as e:
            logger.debug("attach shared fact skipped: %s", e)
