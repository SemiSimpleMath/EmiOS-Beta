"""dayflow_orchestrator.work_portfolio — the STRATEGIC projection of work objects for the dayflow steward
(strategic_planner_wo).

render_work_portfolio(wo) compresses ONE work object's graph into the summary the steward reasons over,
focused on what it needs to judge FIT and trigger a re-plan: a freshness/delta marker, FAILED main tasks (LOUD), the recent OUTCOMES as goal -> finalizer summary
(so it can tell advancing from stuck/sideways), then a compact frontier of main tasks and waits
plus a task census. Owned provenance is read by workers and the finalizer.

work_objects is imported lazily so this module loads even when the substrate is absent.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

_TITLE_CHARS = 90
_BODY_CHARS = 400

# The ONE glossary of node statuses. Prepended to every projection and injected into every agent that
# reads or writes them, so the vocabulary cannot drift between prompts.
#
# It used to omit `closed` — the status the whole completion model turns on, since `is_satisfied`
# requires it of every top-level node and the finalizer is its only producer — while `closed` rendered
# verbatim in the projections agents read. And it taught "done: finished", which is the opposite of
# what `done` means for a top-level node: a result exists and the finalizer has not yet ruled, so it
# counts toward nothing. Both are fixed here; every reader gets the same words.
from app.assistant.dayflow_orchestrator.work_context import render_view

STATUS_LEGEND = render_view("legend")


def _t(n) -> str:
    return (getattr(n, "title", "") or getattr(n, "content", "") or "").strip().replace("\n", " ")[:_TITLE_CHARS]


def _body(n) -> str:
    return (getattr(n, "content", "") or "").strip().replace("\n", " ")[:_BODY_CHARS]


def node_result(wo, node, *, limit: int | None = None) -> str:
    """A node's RESULT is the EVIDENCE it produced — NOT its `content`. `content` is the node's directive,
    its immutable identity; the manager's answer is recorded as evidence/artifact children (a graph row
    under the node). Returns the joined text of those children.

    FULL by default. The result text is what every judgment is made from: the finalizer reads it to
    decide what a node's outcome MEANS, and a failed node's WHY/RESULT is the only account of why the
    goal is blocked. Both were capped at 400 characters for every reader — clipping the two places it
    matters most, while the finalizer's own contract promised the FULL result.

    Pass `limit` where the concern is prompt VOLUME rather than judgment: the OUTCOMES list renders
    every terminal node of every active work object, so it caps; the single node under judgment and
    the failure account do not.
    """
    evidence = [m for m in wo.nodes.values()
                if m.parent_id == node.id and m.type in ("evidence", "artifact") and (m.content or "").strip()]
    epoch = int(node.payload.get("dispatch_epoch") or 0)
    current = [m for m in evidence if m.payload.get("dispatch_epoch") == epoch]
    # Explicit attempt receipts take precedence. Full prior history is rendered separately.
    parts = [(m.content or "").strip() for m in (current or evidence)]
    joined = (" | ".join(parts)).replace("\n", " ")
    return joined[:limit] if limit else joined


def finalizer_summary(node) -> str:
    """The strategic account of a task; raw worker history stays with worker/finalizer."""
    return render_view("finalizer_summary", finalizer=(node.payload or {}).get("finalizer"))


def local_stamp(dt) -> str:
    """A stored (UTC) datetime as the reader's local wall-clock, with the day, e.g. 'Thu 09-17 05:02 PM'.

    Every timestamp an agent reads is local; the store keeps UTC. Rendering the raw value put
    '09-15 05:02' in front of planners and the auditor for an action taken at 10:02 PM the night
    before — which every one of them read as local and reported as a future-dated send.
    """
    if dt is None:
        return ""
    from app.assistant.utils.time_utils import utc_to_local
    try:
        return utc_to_local(dt).strftime("%a %m-%d %I:%M %p")
    except (TypeError, ValueError):
        return str(dt)


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


# --------------------------------------------------------------------------- #
# WORK SIGNAL — is this goal converging, or eating itself?
#
# 2026-09-13: a goal reading "record the October 9 Reflections deadline" answered itself at
# 16:27 with a source URL, delivered that to the user at 16:58, and then grew to 117 nodes
# and ten levels of recursion by 17:40, burning ~$4/hour against a $9.54/day average. Every
# agent that could have stopped it saw a healthy object: the projection lists only
# top-level tasks, so the view was two lines and "progress: 1/2".
#
# Nothing anywhere measured the goal's own behaviour. The finalizer judges ONE node's
# result. The steward judges FIT. Repair only sees FAILED nodes, and nothing had failed —
# every individual step succeeded. So these are structural facts about the graph, not
# judgements: age, size, growth rate, depth, and how much is live. No wording is compared,
# because a churn signal keyed on phrasing would be another wording-based identity.
_SIGNAL_RECENT_MINUTES = 60
_SIGNAL_BURST_MINUTES = 10
# A goal legitimately decomposes a couple of levels. Ten is a worker re-asking its own
# question down a chain. Three is the point where it stops looking like decomposition.
_SIGNAL_DEPTH_WARN = 3
_SIGNAL_RECENT_WARN = 20        # nodes minted in the last hour
_SIGNAL_TOTAL_WARN = 25         # total nodes for one goal
_LIVE_STATUSES = ("proposed", "actionable", "dispatched")


def _depth_below_goal(wo, node, gid) -> int:
    """How many ownership hops from the goal. Direct children are 1. Cycle-safe."""
    depth, seen, cur = 0, set(), node
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        if cur.parent_id is None or cur.parent_id == gid:
            return depth + 1
        cur = wo.nodes.get(cur.parent_id)
        depth += 1
    return depth


def _minutes_since(dt, now) -> Optional[float]:
    if dt is None:
        return None
    try:
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return None


def work_signal(wo, now) -> list:
    """Lines describing whether this goal is converging. Empty for a small, quiet graph."""
    gid = wo.goal_node_id
    spine = [n for n in wo.nodes.values() if n.id != gid and getattr(n, "type", "") == "subtask"]
    if not spine:
        return []

    ages = [_minutes_since(getattr(n, "created_at", None), now) for n in spine]
    recent = sum(1 for a in ages if a is not None and a <= _SIGNAL_RECENT_MINUTES)
    burst = sum(1 for a in ages if a is not None and a <= _SIGNAL_BURST_MINUTES)
    live = [n for n in spine if n.status in _LIVE_STATUSES]
    deepest = max((_depth_below_goal(wo, n, gid) for n in spine), default=0)
    abandoned = sum(1 for n in spine if n.status == "abandoned")
    goal_age = _minutes_since(getattr(wo, "created_at", None), now)
    age_txt = ("%dh %dm" % (int(goal_age // 60), int(goal_age % 60))) if goal_age else "?"

    from app.assistant.dayflow_orchestrator.work_context import render_view
    return render_view("work_signal", age=age_txt, total=len(spine), abandoned=abandoned,
                       recent=recent, deepest=deepest, live=len(live), burst=burst,
                       depth_warn=_SIGNAL_DEPTH_WARN, burst_minutes=_SIGNAL_BURST_MINUTES,
                       recent_warn=_SIGNAL_RECENT_WARN, total_warn=_SIGNAL_TOTAL_WARN).splitlines()



def render_work_portfolio(wo, now=None) -> str:
    """Render the shared strategic view; wording and formatting live in Jinja."""
    from app.assistant.dayflow_orchestrator.work_context import render_view, work_data
    return render_view("portfolio", work=work_data(wo, now))


def render_portfolio(work_objects, now=None) -> str:
    """Render each active work object with the shared status definitions."""
    from app.assistant.dayflow_orchestrator.work_context import render_view, work_data
    return render_view("portfolio_list", works=[work_data(wo, now) for wo in (work_objects or [])])
