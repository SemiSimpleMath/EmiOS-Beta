"""dayflow_orchestrator.work_portfolio — the STRATEGIC projection of work objects for the dayflow steward
(strategic_planner_wo).

render_work_portfolio(wo) compresses ONE work object's graph into the summary the steward reasons over,
focused on what it needs to judge FIT and trigger a re-plan: a freshness/delta marker, FAILED nodes (LOUD,
any depth — these block the goal until adjudicated), the recent OUTCOMES as goal -> agent-facing result
(so it can tell advancing from stuck/sideways), then a compact frontier (top-level tasks / waiting / open
questions) + a census. Deep subtrees stay the worker's concern.

work_objects is imported lazily so this module loads even when the substrate is absent.
"""
from __future__ import annotations

from collections import Counter

_TITLE_CHARS = 90
_BODY_CHARS = 400


def _t(n) -> str:
    return (getattr(n, "title", "") or getattr(n, "content", "") or "").strip().replace("\n", " ")[:_TITLE_CHARS]


def _body(n) -> str:
    return (getattr(n, "content", "") or "").strip().replace("\n", " ")[:_BODY_CHARS]


def _ago(dt, now) -> str:
    """Compact 'Nm ago' / 'Nh ago' if within the last 6h, else '' (stale/unknown). Tolerates bad input."""
    if dt is None:
        return ""
    try:
        secs = (now - dt).total_seconds()
    except Exception:
        return ""
    if secs < 0 or secs > 6 * 3600:
        return ""
    m = int(secs // 60)
    return f"{m}m ago" if m < 90 else f"{m // 60}h ago"


def render_work_portfolio(wo, now=None) -> str:
    """Strategic, fit-focused summary of ONE work object for the dayflow steward. Pure (wo, now) -> str."""
    from work_objects.model import utcnow
    now = now or utcnow()
    goal = wo.nodes.get(wo.goal_node_id or "")
    gid = goal.id if goal else None
    fresh = _ago(getattr(wo, "updated_at", None), now)
    L = [f"=== WORK OBJECT {wo.id} ===" + (f"   [updated {fresh}]" if fresh else "")]
    L.append(f"goal   : {_t(goal) if goal else '(none)'}")
    L.append(f"status : {wo.status}    success-when: {getattr(goal, 'satisfied_when_kind', None)}")

    top = [n for n in wo.nodes.values() if n.parent_id == gid and n.type == "subtask"]
    done_top = [n for n in top if wo.is_satisfied(n)]
    L.append(f"progress: {len(done_top)}/{len(top)} top-level tasks complete")

    # FAILED nodes — LOUD, any depth. A failed node blocks the goal until adjudicated. This is the signal
    # the steward must act on (abandon / re-plan / re-scope).
    failed = [n for n in wo.nodes.values() if n.status == "failed"]
    if failed:
        L.append(f"\n⚠ FAILED ({len(failed)}) — goal is BLOCKED until adjudicated (abandon / re-plan / re-issue):")
        for n in failed:
            L.append(f"  - GOAL: {_t(n)}")
            b = _body(n)
            if b:
                L.append(f"    WHY/RESULT: {b}")

    # RECENT OUTCOMES — the delta: goal -> agent-facing result, newest first. Lets the steward judge whether
    # each finished node ADVANCED the work object's goal (toward / sideways / wall).
    terminal = [n for n in wo.nodes.values()
                if n.id != gid and n.status != "failed" and getattr(n, "is_terminal", False)
                and (n.content or n.pod_ref)]
    terminal.sort(key=lambda n: getattr(n, "updated_at", now) or now, reverse=True)
    if terminal:
        L.append(f"\nRECENT OUTCOMES (goal -> result; newest {min(5, len(terminal))} of {len(terminal)}):")
        for n in terminal[:5]:
            L.append(f"  - [{n.status}] GOAL: {_t(n)}")
            b = _body(n)
            L.append(f"    RESULT: {b if b else ('(pod ' + n.pod_ref + ')' if n.pod_ref else '(no detail)')}")

    # TASKS — compact frontier of what's left, with a failed flag inline.
    if top:
        L.append("\nTASKS (top-level; subtrees handled by the worker):")
        for n in top:
            kids = [wo.nodes[c] for c in wo.children_of(n.id) if c in wo.nodes]
            kd = sum(1 for k in kids if wo.is_satisfied(k))
            roll = f" [{kd}/{len(kids)} sub]" if kids else ""
            unmet = [d for d in wo.deps_of(n.id) if d in wo.nodes and not wo.is_satisfied(wo.nodes[d])]
            blk = f" BLOCKED({len(unmet)} dep)" if unmet else ""
            flag = " ⚠FAILED" if n.status == "failed" else ""
            L.append(f"  - [{n.status}]{roll}{blk}{flag} {_t(n)}")

    waiting = [n for n in wo.nodes.values()
               if n.status == "waiting" or (n.wake_at is not None and n.wake_at > now)]
    if waiting:
        L.append("\nWAITING (parked):")
        for n in waiting:
            when = f"  (wakes {n.wake_at.isoformat()})" if n.wake_at else ""
            L.append(f"  - {_t(n)}{when}")

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
