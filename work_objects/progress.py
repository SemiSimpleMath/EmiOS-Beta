"""Where a work object stands, for readers outside dayflow (2026-10-04).

Owner, 2026-10-04: "So they don't quite speak the same language and it is ambiguous. because if it says not
sent. then i too unless i knew that the dayflow works it until either closed or abandoned with reason. so
either whats written into the wo has to be clearer to an outside reader (prefer) or it has to be explained
in a skill or prompt." (docs/design/work_progress_for_outside_readers_2026-10-04.md)

Dayflow's own agents read a work object's tasks, statuses and waits (shared/work/portfolio.j2). The brain,
the gate and the concern planner read only the finalizer's judgments, each about one task, so "the draft
remains unsent for the separate delivery step" read as the state of the whole work while the delivery was
queued. `work_progress` says what the work has done, what it will still do and what each remaining step
waits for, from the store's main tasks; shared/work/work_progress.j2 words it.

Main tasks only (`WorkObject.is_work_unit`), as the architect sees them; worker provenance stays out.
Pure: reads the work object, writes nothing. Every main-task status has a place here; an unknown one raises.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from work_objects.model import WorkObject, WorkNode, utcnow

# The work object's own status -> state. `blocked` is a stored container status the store accepts.
_WORK_STATES = {"active": "in_progress", "blocked": "on_hold", "done": "done", "abandoned": "abandoned"}
_DROPPED = {"abandoned", "superseded"}
_OPEN = {"proposed", "actionable", "waiting", "dispatched", "done", "failed"}


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _ask(wo: WorkObject, node: WorkNode) -> Dict[str, Any]:
    """The question an open ask put to the owner and when: the newest ticket the task sent."""
    sent = [a for a in wo.actions if a.node_id == node.id and a.channel == "ticket"]
    latest = max(sent, key=lambda a: a.ts) if sent else None
    return {"question": latest.summary if latest else None, "asked_at": _iso(latest.ts) if latest else None}


def _wait(wo: WorkObject, node: WorkNode, now: datetime) -> Dict[str, Any]:
    """What one open main task waits for, as a kind the template words."""
    status = node.status
    if status == "dispatched":
        if node.wake_kind == "user_reply":
            return {"kind": "owner_reply", **_ask(wo, node)}
        return {"kind": "being_done"}
    if status == "done":
        return {"kind": "being_checked"}
    if status == "failed":
        return {"kind": "failed"}
    if status in {"proposed", "actionable", "waiting"}:
        if node.wake_kind in {"event", "signal"}:
            return {"kind": "watching", "condition": node.wake_ref}
        if node.wake_at is not None and node.wake_at > now:
            return {"kind": "scheduled", "at": _iso(node.wake_at)}
        after = [wo.nodes[d].title for d in wo.deps_of(node.id)
                 if d in wo.nodes and not wo.is_satisfied(wo.nodes[d])]
        if after:
            return {"kind": "after", "tasks": after}
        return {"kind": "ready"}
    raise ValueError(f"work_progress: task {node.id} has status {status!r}, which has no wording")


def _plan_order(wo: WorkObject, tasks: List[WorkNode]) -> List[WorkNode]:
    """Main tasks in the order the plan runs them: after the tasks they depend on, then by creation
    (the architect often creates a whole plan at one instant)."""
    depth: Dict[str, int] = {}

    def of(node_id: str, path: tuple) -> int:
        if node_id in path:
            raise ValueError(f"work_progress: dependency cycle in {wo.id} through {node_id}")
        if node_id not in depth:
            deps = [d for d in wo.deps_of(node_id) if d in wo.nodes]
            depth[node_id] = 1 + max((of(d, path + (node_id,)) for d in deps), default=-1)
        return depth[node_id]

    return sorted(tasks, key=lambda n: (of(n.id, ()), n.created_at))


def work_progress(wo: WorkObject, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The work's state, its finished and dropped steps, and each step still to come with what it waits
    for. Times are UTC ISO strings; readers show them local."""
    now = now or utcnow()
    if wo.status not in _WORK_STATES:
        raise ValueError(f"work_progress: work {wo.id} has status {wo.status!r}, which has no wording")
    goal = wo.nodes.get(wo.goal_node_id)
    terminal = (goal.payload.get("terminal") or {}) if goal else {}
    state = _WORK_STATES[wo.status]
    ended = state in {"done", "abandoned"}
    done: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    to_come: List[Dict[str, Any]] = []
    unfinished: List[Dict[str, Any]] = []      # open when the work ended (older work); nothing more comes of it
    for node in _plan_order(wo, [n for n in wo.nodes.values() if wo.is_work_unit(n)]):
        fin = node.payload.get("finalizer") or {}
        if node.status == "closed":
            done.append({"title": node.title, "verdict": fin.get("verdict"), "at": fin.get("at")})
        elif node.status in _DROPPED and (node.payload.get("terminal") or {}).get("verdict") == "cascade":
            unfinished.append({"title": node.title})     # closed by the work's own ending, whose reason is said once
        elif node.status in _DROPPED:
            dropped.append({"title": node.title, "reason": (node.payload.get("terminal") or {}).get("reason")})
        elif node.status in _OPEN:
            if ended:
                unfinished.append({"title": node.title})
            else:
                to_come.append({"title": node.title, "wait": _wait(wo, node, now)})
        else:
            raise ValueError(f"work_progress: task {node.id} has status {node.status!r}, which has no wording")
    return {"work_id": wo.id, "state": state,
            "ended_at": _iso(terminal.get("at") or wo.updated_at) if ended else None,
            "reason": terminal.get("reason") if ended else None,
            "replanning": not ended and wo.has_pending_revision(),
            "done": done, "dropped": dropped, "to_come": to_come, "unfinished": unfinished}
