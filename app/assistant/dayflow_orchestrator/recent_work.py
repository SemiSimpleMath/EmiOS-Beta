"""Recently ended work objects, as their readers see them: the full objective, when it ended (local) and why.

The architect reads the work completed recently beside the goal it plans; work_links reads a work object's ending
reason. Moved here on 2026-10-10 from the steward's prep node (strategic_planner_wo_prep_node), retired with the
steward: the steward read these logs so as not to recreate work it had just finished or dropped.
"""
from app.assistant.dayflow_orchestrator.work_context import render_view
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def goal_epitaph(wo) -> str:
    """The work object's ending reason: the goal node's payload.terminal.reason, where set_work_status records WHY
    the object ended (drop reasons, finalizer resolve reasoning, hand-written DO-NOT-RECREATE epitaphs)."""
    nodes = getattr(wo, "nodes", {}) or {}
    goal = nodes.get(getattr(wo, "goal_node_id", "") or "")
    term = (getattr(goal, "payload", {}) or {}).get("terminal") if goal is not None else None
    return str(term.get("reason") or "").strip().replace("\n", " ") if isinstance(term, dict) else ""


def ended_item(wo, *, finalizers: bool) -> dict:
    """One recently ended work object: its FULL objective (the goal node's content; the stored title is the objective
    cut at 80 characters), when it ended in local time, and why.

    The end time is the goal's `terminal.at`, which every ending writes (set_work_status, the rollup, a terminal
    set_status); an ended goal without one raises."""
    from app.assistant.utils.time_utils import local_time_text

    goal = wo.nodes.get(wo.goal_node_id)
    terminal = (goal.payload.get("terminal") if goal is not None else None) or {}
    if not terminal.get("at"):
        raise ValueError(f"ended work object {wo.id} records no end time on its goal")
    return {"id": wo.id, "title": (goal.content or goal.title).strip(), "when": local_time_text(terminal["at"]),
            "reason": goal_epitaph(wo),
            "finalizers": [task.payload["finalizer"] for task in wo.nodes.values()
                           if wo.is_work_unit(task) and task.payload.get("finalizer")] if finalizers else []}


def recent_ended_items(summaries, store, window_hours=18):
    """Two separate logs of recent terminal work objects: DONE = finished; ABANDONED = dropped, not finished, each
    with the finalizers' accounts (the user's last recorded reply). Every entry shows the full objective and when it
    ended, in local time. A record that cannot be read is logged as an ERROR and marked unreadable. Returns (done
    items, abandoned items) for render_recent_ended; summaries are newest-updated first."""
    from datetime import datetime, timedelta, timezone

    from app.assistant.utils.time_utils import parse_iso_utc

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    done, abandoned = [], []
    for s in summaries:
        upd = parse_iso_utc(str(s.get("updated_at") or ""))
        if upd is not None and upd < cutoff:
            break   # newest-first → everything after is older than the window
        status = str(s.get("status") or "").lower()
        if status not in ("done", "abandoned"):
            continue
        try:
            item = ended_item(store.load(s["id"]), finalizers=status == "abandoned")
        except Exception as e:
            logger.error("[recent work] ended work object %s could not be read for the recent-work log: %s",
                         s.get("id"), e)
            item = {"id": str(s.get("id")), "title": str(s.get("title") or "").strip(), "unreadable": str(e)}
        (done if status == "done" else abandoned).append(item)
    return done, abandoned


def render_recent_ended(done, abandoned, window_hours=18, labels=None):
    """(done_str, abandoned_str); `labels` (id -> label of the call) shows each work object's label."""
    return (render_view("recent_work", items=done, dropped=False, window_hours=window_hours,
                        labels=labels or {}).strip(),
            render_view("recent_work", items=abandoned, dropped=True, window_hours=window_hours,
                        labels=labels or {}).strip())
