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

# A compact glossary of node statuses, prepended to projections so agents reason over the vocabulary
# unambiguously (esp. `dispatched`, which means in-flight, NOT "open/available").
STATUS_LEGEND = (
    "NODE STATUS KEY — proposed: planned, sitting in the architect's inbox (not yet approved to run). "
    "actionable: the state_mover approved it; it is queued for the action_selector to dispatch. "
    "dispatched: IN-FLIGHT — a worker is on it, or an ask is out (do not re-dispatch). "
    "waiting: parked on a time / event / dependency gate. done: finished. "
    "failed: the step broke and is awaiting work_repair. abandoned: dropped."
)


def _t(n) -> str:
    return (getattr(n, "title", "") or getattr(n, "content", "") or "").strip().replace("\n", " ")[:_TITLE_CHARS]


def _body(n) -> str:
    return (getattr(n, "content", "") or "").strip().replace("\n", " ")[:_BODY_CHARS]


def node_result(wo, node) -> str:
    """A node's RESULT is the EVIDENCE it produced — NOT its `content`. `content` is the node's directive,
    its immutable identity; the manager's answer is recorded as evidence/artifact children (a graph row
    under the node). Returns the joined text of those children."""
    parts = [(m.content or "").strip() for m in wo.nodes.values()
             if m.parent_id == node.id and getattr(m, "type", "") in ("evidence", "artifact")
             and (m.content or "").strip()]
    return (" | ".join(parts)).replace("\n", " ")[:_BODY_CHARS]


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
    # Unrun tasks, LOUD on their own line: completing the work object now discards them.
    # 2026-08-20: the steward completed a review WO whose give-the-user-the-assessment
    # hand-off was still `proposed` — the assessment existed on the graph but never
    # reached the user. Produced is not delivered.
    unrun = [n for n in top
             if n.status not in ("done", "closed", "verified", "passed", "abandoned", "failed")]
    if unrun:
        L.append(f"STILL UNRUN ({len(unrun)}) — completing this work object now would DISCARD these:")
        for n in unrun:
            L.append(f"  - [{n.status}] {_t(n)}")

    # FAILED nodes — LOUD, any depth. A failed node blocks the goal until adjudicated. This is the signal
    # the steward must act on (abandon / re-plan / re-scope).
    failed = [n for n in wo.nodes.values() if n.status == "failed"]
    if failed:
        L.append(f"\n⚠ FAILED ({len(failed)}) — goal is BLOCKED until adjudicated (abandon / re-plan / re-issue):")
        for n in failed:
            L.append(f"  - {_t(n)}")
            why = node_result(wo, n)
            if why:
                L.append(f"    WHY/RESULT: {why}")

    # RECENT OUTCOMES — the delta: goal -> agent-facing result, newest first. Lets the steward judge whether
    # each finished node ADVANCED the work object's goal (toward / sideways / wall).
    terminal = [n for n in wo.nodes.values()
                if n.id != gid and n.status != "failed" and getattr(n, "is_terminal", False)]
    terminal.sort(key=lambda n: getattr(n, "updated_at", now) or now, reverse=True)
    shown = [(n, node_result(wo, n)) for n in terminal]
    shown = [(n, r) for n, r in shown if r or n.pod_ref]   # only nodes that actually produced something
    if shown:
        L.append(f"\nOUTCOMES (node -> result; {len(shown)}):")
        for n, r in shown:
            L.append(f"  - [{n.status}] {_t(n)}")
            L.append(f"    RESULT: {r if r else ('pod ' + n.pod_ref)}")

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
            term = (n.payload or {}).get("terminal")
            why = f"  — why: {str(term.get('reason'))[:100]}" if isinstance(term, dict) else ""
            L.append(f"  - [{n.status}]{roll}{blk}{flag} {_t(n)}{why}")

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
    return STATUS_LEGEND + "\n\n" + "\n\n".join(render_work_portfolio(wo, now) for wo in work_objects)
