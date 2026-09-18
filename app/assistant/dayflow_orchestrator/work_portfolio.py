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
STATUS_LEGEND = (
    "NODE STATUS KEY — proposed: planned, sitting in the architect's inbox (not yet approved to run). "
    "actionable: approved and QUEUED — one node dispatches per tick, so it is waiting its turn; queued "
    "is NOT stalled. dispatched: IN-FLIGHT — a worker is on it, or an ask is out (do not re-dispatch). "
    "waiting: held ON PURPOSE on a time / event / dependency gate until its wake; held is NOT stalled. "
    "done: it produced a RESULT and the work_finalizer has not judged it yet — a top-level node counts "
    "toward its goal only once the finalizer closes it. closed: judged and counted — the satisfied "
    "terminal, and the only one that completes a goal. failed: judged NOT ACHIEVED — the finalizer's "
    "outcome and recommendation are on the node (retry / new approach / stop / ask the user) and the "
    "architect acts on them next tick. abandoned: dropped. superseded: replaced by newer work."
)


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
    parts = [(m.content or "").strip() for m in wo.nodes.values()
             if m.parent_id == node.id and getattr(m, "type", "") in ("evidence", "artifact")
             and (m.content or "").strip()]
    joined = (" | ".join(parts)).replace("\n", " ")
    return joined[:limit] if limit else joined


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

    out = [f"\nWORK SIGNAL: age {age_txt} | {len(spine)} subtasks "
           f"({abandoned} abandoned) | {recent} created in the last hour | "
           f"depth {deepest} | {len(live)} live"]

    # Warnings name the number that tripped them, so the reader can judge rather than obey.
    if deepest > _SIGNAL_DEPTH_WARN:
        out.append(f"  ⚠ work is {deepest} levels below the goal. Past about "
                   f"{_SIGNAL_DEPTH_WARN} that is a worker re-asking its own question, not "
                   f"decomposition. Read the OUTCOMES above: the answer may already be there.")
    if burst >= _SIGNAL_BURST_MINUTES:
        out.append(f"  ⚠ {burst} subtasks minted in the last {_SIGNAL_BURST_MINUTES} minutes. "
                   f"This goal is growing faster than it is finishing.")
    if recent > _SIGNAL_RECENT_WARN:
        out.append(f"  ⚠ {recent} subtasks in the last hour. A goal that needs this many "
                   f"steps has either changed shape or is not converging.")
    if len(spine) > _SIGNAL_TOTAL_WARN:
        out.append(f"  ⚠ {len(spine)} subtasks for one goal. Consider whether it is done "
                   f"already, or genuinely too big and should be split or abandoned.")
    return out


def render_work_portfolio(wo, now=None) -> str:
    """Strategic, fit-focused summary of ONE work object for the dayflow steward. Pure (wo, now) -> str."""
    from work_objects.model import utcnow
    now = now or utcnow()
    goal = wo.nodes.get(wo.goal_node_id or "")
    gid = goal.id if goal else None
    fresh = _ago(getattr(wo, "updated_at", None), now)
    L = [f"=== WORK OBJECT {wo.id} ===" + (f"   [updated {fresh}]" if fresh else "")]
    # The goal renders from CONTENT, not title. `title` is `objective[:80]` (work_persist),
    # and on 2026-09-12 that slice ate the deadline off a flea-medication goal: the stored
    # title ended "...around September" while the content named both September 25 and the
    # September 18 decision date. An hour of replanning then argued over dates that were
    # sitting unread one field away. Same rule as the epitaph below — decision input is
    # never truncated.
    goal_text = ((getattr(goal, "content", "") or "").strip()
                 or (getattr(goal, "title", "") or "").strip()) if goal else ""
    L.append(f"goal   : {' '.join(goal_text.split()) if goal_text else '(none)'}")
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
        L.append(f"STILL UNRUN ({len(unrun)}) — queued/held; the runtime will run them (not stalled). "
                 "Completing this work object now would DISCARD these:")
        for n in unrun:
            L.append(f"  - [{n.status}] {_t(n)}")

    # FAILED nodes — LOUD, any depth. A failed node blocks the goal until adjudicated. This is the signal
    # the steward must act on (abandon / re-plan / re-scope).
    failed = [n for n in wo.nodes.values() if n.status == "failed"]
    if failed:
        L.append(f"\n⚠ FAILED ({len(failed)}) — goal is BLOCKED until adjudicated (abandon / re-plan / re-issue):")
        for n in failed:
            L.append(f"  - {_t(n)}")
            # How many times THIS step has failed. One failure is bad luck; the same step failing
            # again and again is the step being wrong, and nothing used to say so — every failure
            # read as a first failure to every agent that saw it.
            repeats = int((n.payload or {}).get("failure_count") or 0)
            if repeats >= 2:
                L.append(f"    ⚠ THIS STEP HAS FAILED {repeats} TIMES — it keeps failing at the "
                         f"same thing. Retrying it will not help: drop it, or have the user asked "
                         f"for whatever would unblock it.")
            why = node_result(wo, n)
            if why:
                L.append(f"    WHY/RESULT: {why}")
            # The finalizer's judgment of that result — the part the steward could not see before,
            # so a step needing the user rendered as a bare failure with the worker's evidence only.
            fin = (n.payload or {}).get("finalizer")
            if isinstance(fin, dict) and str(fin.get("outcome") or "").strip():
                route = str(fin.get("next_step") or "").strip()
                L.append(f"    FINALIZER ({fin.get('verdict')}{' -> ' + route if route else ''}): "
                         f"{fin.get('outcome')}")
                if str(fin.get("recommendation") or "").strip():
                    L.append(f"    RECOMMENDS: {fin.get('recommendation')}")
                if str(fin.get("question_for_user") or "").strip():
                    L.append(f"    ASK THE USER: {fin.get('question_for_user')}")

    # RECENT OUTCOMES — the delta: goal -> agent-facing result, newest first. Lets the steward judge whether
    # each finished node ADVANCED the work object's goal (toward / sideways / wall).
    terminal = [n for n in wo.nodes.values()
                if n.id != gid and n.status != "failed" and getattr(n, "is_terminal", False)]
    terminal.sort(key=lambda n: getattr(n, "updated_at", now) or now, reverse=True)
    shown = [(n, node_result(wo, n, limit=_BODY_CHARS)) for n in terminal]
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
            # The epitaph is NEVER truncated — it is decision input (owner ruling,
            # repeated: cut-off reasons in prompts caused recreate/replan churn).
            why = f"  — why: {term.get('reason')}" if isinstance(term, dict) else ""
            L.append(f"  - [{n.status}]{roll}{blk}{flag} {_t(n)}{why}")

    # Live nodes only. A future wake_at on an abandoned node is a leftover, not a plan — it read
    # as "WAITING (parked)" to the steward until 2026-09-18.
    waiting = [n for n in wo.nodes.values()
               if n.status == "waiting"
               or (n.status in ("proposed", "actionable") and n.wake_at is not None and n.wake_at > now)]
    if waiting:
        L.append("\nWAITING (parked):")
        for n in waiting:
            when = f"  (wakes {local_stamp(n.wake_at)})" if n.wake_at else ""
            L.append(f"  - {_t(n)}{when}")

    questions = [n for n in wo.nodes.values() if n.type == "question" and n.status == "open"]
    if questions:
        L.append("\nOPEN QUESTIONS:")
        for n in questions:
            L.append(f"  - {_t(n)}")

    # ACTIONS TAKEN — this goal's past tense, newest last so it reads as a timeline.
    # The single most load-bearing block here: everything else describes what is PLANNED,
    # and a pass that cannot see what it already DID will do it again. Never truncated,
    # never filtered by outcome — an expired ask is exactly the one you must not repeat
    # silently. Repeats to one target are called out, because eleven near-identical emails
    # to one person in an hour read as eleven ordinary rows otherwise.
    if getattr(wo, "actions", None):
        acts = sorted(wo.actions, key=lambda a: getattr(a, "ts", now) or now)
        repeats = Counter((a.channel, (a.target or "").lower()) for a in acts)
        L.append(f"\nACTIONS TAKEN ({len(acts)}) — what this goal has already DONE to the "
                 "outside world. Do not repeat one of these without a reason that names it:")
        for a in acts:
            when = local_stamp(getattr(a, "ts", None))
            L.append(f"  {when}  {a.channel} -> {a.target or '?'}  [{a.outcome}]  {a.summary}")
        hot = [(c, t, n) for (c, t), n in repeats.items() if n >= 3]
        for channel, target, n in sorted(hot, key=lambda x: -x[2]):
            L.append(f"  ⚠ {n} separate {channel} messages to {target} on this goal alone. "
                     "Asking again is very unlikely to be the missing step.")

    L.extend(work_signal(wo, now))

    census = Counter(f"{n.type}/{n.status}" for n in wo.nodes.values())
    L.append("\ncensus: " + ", ".join(f"{k}={v}" for k, v in sorted(census.items())))
    return "\n".join(L)


def render_portfolio(work_objects, now=None) -> str:
    """Render a list of work objects as the dayflow planner's full portfolio (one block each)."""
    work_objects = list(work_objects or [])
    if not work_objects:
        return "(no active work objects)"
    return STATUS_LEGEND + "\n\n" + "\n\n".join(render_work_portfolio(wo, now) for wo in work_objects)
