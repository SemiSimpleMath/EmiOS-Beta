"""
WorkObjectRenderNode — the pre-node that runs before every WorkerPlanner turn.

It loads the WorkObject node the planner owns and renders it (its node, its checklist
= child subtask nodes, recorded outputs, resolved dependencies, and the work tree)
into `work_projection` on the blackboard; the planner's user.j2 renders that. Re-runs
each cycle so the planner always sees the CURRENT node state (subtasks it just added,
statuses it just changed) — current-state + the inherited recent_history together.

The render is also exposed as the module function `render_work_projection(wo, node_id)`
so the finalizer (WorkFinalAnswer) can re-render the SAME view fresh right before it runs
— after the planner's final findings write — without a second control node.

work_objects is imported LAZILY so this node loads at boot even when work_objects is
absent; it only does real work inside a WorkObject run (where a WorkContext is set).
"""
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _node_detail(node) -> str:
    """A node's result detail for the projection: its content, else — if the result lives in a pod —
    the pod's SUMMARY (one_liner), never a bare datapod:... id. So a viewer always sees WHAT a node
    produced."""
    detail = (getattr(node, "content", "") or "").strip()
    if not detail and getattr(node, "pod_ref", None):
        try:
            from work_objects.work_tools import pod_summary
            detail = pod_summary(node.pod_ref)
        except Exception:
            detail = str(node.pod_ref)
    return detail


def render_work_projection(wo, node_id: str) -> str:
    """Render a WorkObject node for a planner/finalizer prompt: its task, RELEVANT INFO,
    checklist, recorded outputs, dependencies, and the work tree. Pure (wo, node_id) -> str,
    so the render control node AND the finalizer's fresh re-render share one source of truth."""
    n = wo.nodes[node_id]
    L: list[str] = []

    L.append("## YOUR NODE  (the task you own)")
    L.append(f"type: {n.type}    status: {n.status}")
    L.append(f"title: {n.title}")
    if n.content:
        L.append(f"task: {n.content}")
    L.append(f"success when: {n.satisfied_when_kind}")
    goal = wo.nodes.get(wo.goal_node_id or "")
    if goal is not None and goal.id != n.id:
        L.append(f"parent goal: {goal.title}")

    # RELEVANT INFO = fact nodes that `informs` THIS node or the GOAL (goal = shared with all)
    goal_id = wo.goal_node_id or ""
    informer_ids, seen = [], set()
    for e in wo.edges:
        if e.relation == "informs" and e.dst in {node_id, goal_id} and e.src not in seen:
            seen.add(e.src)
            informer_ids.append(e.src)
    if informer_ids:
        L.append("")
        L.append("## RELEVANT INFO  (facts + committed decisions surfaced for you by the curator/router/director — use them, do NOT re-derive)")
        for fid in informer_ids:
            f = wo.nodes.get(fid)
            if f is None:
                continue
            detail = _node_detail(f)
            if detail:
                L.append(f"- {detail}")

    # YOUR CHECKLIST = this node's child subtasks
    kids = [m for m in wo.nodes.values() if m.parent_id == node_id and m.type == "subtask"]
    L.append("")
    L.append("## YOUR CHECKLIST  (echo [id:…] to keep/update an item, omit id to ADD one; "
             "mark done only AFTER its result is in recent history)")
    if kids:
        for k in kids:
            L.append(f"- [id:{k.id}] [{k.status}] {k.title}")
    else:
        L.append("(empty — list this node's subtasks here, or if it's a single step just do it and finish)")

    # Outputs recorded on THIS node so far (the planner's findings + any artifacts)
    outs = [m for m in wo.nodes.values() if m.parent_id == node_id and m.type in {"evidence", "artifact"}]
    if outs:
        L.append("")
        L.append("## RECORDED ON THIS NODE")
        for o in outs:
            detail = _node_detail(o)
            L.append(f"- [{o.type}] {o.title}" + (f" -> {detail}" if detail else ""))

    # DEPENDENCIES — upstream nodes' produced outputs (content inline; no peeking)
    dep_ids = [e.src for e in wo.edges if e.dst == node_id and e.relation == "depends_on"]
    if dep_ids:
        L.append("")
        L.append("## DEPENDENCIES  (already solved — use these directly; do NOT peek them)")
        for did in dep_ids:
            d = wo.nodes.get(did)
            if d is None:
                continue
            L.append(f"- {d.title}:")
            for e in wo.edges:
                if e.src == did and e.relation == "produces" and e.dst in wo.nodes:
                    p = wo.nodes[e.dst]
                    detail = _node_detail(p)
                    L.append(f"    * [{p.type}] {p.title}" + (f" -> {detail}" if detail else ""))

    # THE WORK TREE (summaries; mark YOU ARE HERE)
    L.append("")
    L.append("## THE WORK TREE  (other nodes' work; a node tagged ·has-output holds a result you "
             "can HYDRATE with work_graph_peek(node_id) — reuse prior work instead of re-deriving it)")
    L.extend(_render_tree(wo, node_id))
    return "\n".join(L)


def _render_tree(wo, here_id) -> list[str]:
    out: list[str] = []

    def walk(node, depth):
        here = node.id == here_id
        marker = "   <- YOU ARE HERE" if here else ""
        out_tag = " ·has-output" if (not here and (node.content or node.pod_ref)) else ""
        out.append(f"  {'  ' * depth}- [{node.type}/{node.status}] (id:{node.id}){out_tag} "
                   f"{(node.title or '')}{marker}")
        for c in wo.nodes.values():
            if c.parent_id == node.id:
                walk(c, depth + 1)

    # fact + decision nodes (root `evidence`/`note`/`decision`) live outside the work spine —
    # they show in RELEVANT INFO via `informs` edges, not as tree roots.
    for r in [x for x in wo.nodes.values()
              if not x.parent_id and x.type not in {"evidence", "artifact", "note", "decision"}]:
        walk(r, 0)
    return out


class WorkObjectRenderNode(ControlNode):
    def action_handler(self, message):
        try:
            projection = self._render()
        except Exception as e:
            logger.error("[%s] WorkObject render failed: %s", self.name, e)
            logger.debug("[%s] render exception", self.name, exc_info=True)
            projection = ""
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
