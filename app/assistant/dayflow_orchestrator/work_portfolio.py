"""dayflow_orchestrator.work_portfolio — the STRATEGIC projection of work objects for the dayflow
planner (strategic_planner_wo).

render_work_portfolio(wo) compresses ONE work object's graph into a compact portfolio summary the
steward reasons over: goal, status, progress, top-level tasks (with subtree rollup + blockers),
parked/waiting work, committed decisions, produced outcomes, open questions, and a completeness census.
The deep subtrees are the worker's concern (workobject_render_node.render_work_projection) and are
deliberately NOT expanded here.

Validated in Phase 0 — gpt-5.4-mini reasons correctly over this view (done->close, in-progress->advance,
delegating the HOW to the worker). See scratch/DAYFLOW_WORKOBJECT_SCOPING.md.

work_objects is imported lazily so this module loads even when the substrate is absent.
"""
from __future__ import annotations

from collections import Counter


def _t(n) -> str:
    return (getattr(n, "title", "") or getattr(n, "content", "") or "").strip().replace("\n", " ")[:74]


def render_work_portfolio(wo, now=None) -> str:
    """Compact strategic summary of ONE work object for the dayflow steward. Pure (wo, now) -> str."""
    from work_objects.model import utcnow
    now = now or utcnow()
    goal = wo.nodes.get(wo.goal_node_id or "")
    L = [f"=== WORK OBJECT {wo.id} ==="]
    L.append(f"goal   : {_t(goal) if goal else '(none)'}")
    L.append(f"status : {wo.status}    success-when: {getattr(goal, 'satisfied_when_kind', None)}")

    top = [n for n in wo.nodes.values() if goal and n.parent_id == goal.id and n.type == "subtask"]
    done_top = [n for n in top if wo.is_satisfied(n)]
    L.append(f"progress: {len(done_top)}/{len(top)} top-level tasks complete")

    if top:
        L.append("\nTASKS (top-level; subtrees handled by the worker):")
        for n in top:
            kids = [wo.nodes[c] for c in wo.children_of(n.id) if c in wo.nodes]
            kd = sum(1 for k in kids if wo.is_satisfied(k))
            roll = f" [{kd}/{len(kids)} sub]" if kids else ""
            unmet = [d for d in wo.deps_of(n.id) if d in wo.nodes and not wo.is_satisfied(wo.nodes[d])]
            blk = f" BLOCKED({len(unmet)} dep)" if unmet else ""
            L.append(f"  - [{n.status}]{roll}{blk} {_t(n)}")

    waiting = [n for n in wo.nodes.values()
               if n.status == "waiting" or (n.wake_at is not None and n.wake_at > now)]
    if waiting:
        L.append("\nWAITING (parked):")
        for n in waiting:
            when = f"  (wakes {n.wake_at.isoformat()})" if n.wake_at else ""
            L.append(f"  - {_t(n)}{when}")

    decisions = [n for n in wo.nodes.values() if n.type == "decision"]
    if decisions:
        L.append("\nDECISIONS committed:")
        for n in decisions:
            L.append(f"  - {_t(n)}")

    outcomes = [n for n in wo.nodes.values()
                if n.type in ("evidence", "artifact") and (n.content or n.pod_ref)]
    if outcomes:
        L.append(f"\nOUTCOMES ({len(outcomes)} findings/artifacts; latest 6):")
        for n in outcomes[-6:]:
            L.append(f"  - [{n.type}] {_t(n)}")

    questions = [n for n in wo.nodes.values() if n.type == "question" and n.status == "open"]
    if questions:
        L.append("\nOPEN QUESTIONS:")
        for n in questions:
            L.append(f"  - {_t(n)}")

    census = Counter(f"{n.type}/{n.status}" for n in wo.nodes.values())
    L.append("\ncensus: " + ", ".join(f"{k}={v}" for k, v in sorted(census.items())))
    return "\n".join(L)


def render_portfolio(work_objects, now=None) -> str:
    """Render a list of work objects as the dayflow planner's full portfolio (one block each)."""
    work_objects = list(work_objects or [])
    if not work_objects:
        return "(no active work objects)"
    return "\n\n".join(render_work_portfolio(wo, now) for wo in work_objects)
