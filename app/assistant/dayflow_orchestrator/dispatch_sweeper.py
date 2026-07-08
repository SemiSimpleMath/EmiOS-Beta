"""Active-dispatch state: stale-dispatch sweep + read-only listing.

The dayflow_orchestrator tracks in-flight manager dispatches as
dayflow_items with ``source_type=action_dispatch`` and
``dispatch_status=in_flight``. This module owns two concerns that used to
live in ``blackboard_builder._load_active_dispatches``:

1. ``sweep_stale_dispatches`` — closes dispatches whose manager never
   reported back (no active invocation + soft timeout, or hard timeout
   regardless). Revives the acted_on source item only if it is still in
   ``dispatched`` state. Runs as a side effect during the cadence tick.

2. ``list_active_dispatches`` — read-only loader that returns a compact
   list of in-flight dispatch rows for view_materializer to filter
   actionable items already covered by an in-flight dispatch.

Keeping the side-effect path separate from the read path removes the
need to carry ``active_action_dispatches`` across the tick->manager
boundary via ``_blackboard_extras``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.dayflow_orchestrator.contracts import get_meta, long_to_short
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc_strict

logger = get_logger(__name__)


_DISPATCH_SOFT_TIMEOUT_MINUTES: int = 10
_DISPATCH_HARD_TIMEOUT_HOURS: int = 2
_DISPATCH_LIST_CAP: int = 40

# Backstop sweep: any source-task stuck in state='dispatched' beyond
# this window with no live dispatch row pointing at it gets revived
# to 'actionable'. Catches state-machine gaps where the dispatch row
# closed cleanly but the source-task revive step crashed (or where a
# task was set to dispatched without a paired dispatch row at all).
_TASK_DISPATCHED_TIMEOUT_HOURS: int = 2

# Zombie waiting sweep: items in state='waiting' whose reactivate_at_utc
# is more than this many hours past — and which therefore have aged out
# of the 24h freshness filter that the cleaner / state_mover see — get
# closed. Without this, a waiting item whose timer fires but doesn't get
# picked up by action_selector stays in waiting forever; the dayflow
# scheduler hot-loops on its stale timer (now patched separately) and
# the cleaner can't see it because get_dayflow_items has its own 24h
# cutoff. The cleaner closes those that are still visible; this sweep
# closes those that aren't.
_WAITING_ZOMBIE_OVERDUE_HOURS: int = 36


def _find_item_by_id(items: List[Dict[str, Any]], item_id: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = get_meta(item)
        if str(meta.get("item_id") or item.get("id") or "").strip() == item_id:
            return item
    return None


def _iter_in_flight_dispatches(all_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in all_items:
        if not isinstance(item, dict):
            continue
        meta = get_meta(item)
        if str(meta.get("source_type") or "").strip().lower() != "action_dispatch":
            continue
        if str(meta.get("dispatch_status") or "").strip().lower() != "in_flight":
            continue
        out.append(item)
    return out


def _active_manager_names() -> set[str]:
    names: set[str] = set()
    try:
        invocation_status = DI.manager_invoker.get_invocation_status()
        for inv in invocation_status.get("active_invocations", []):
            name = str(inv.get("manager_name") or "").strip().lower()
            if name:
                names.add(name)
    except Exception:
        logger.error(
            "dispatch_sweeper: could not read invocation status — sweep may "
            "incorrectly revive in-flight items",
            exc_info=True,
        )
    return names


def _dispatch_age(meta: Dict[str, Any], now_utc: datetime) -> Optional[timedelta]:
    created_at_str = str(meta.get("created_at") or "").strip()
    if not created_at_str:
        return None
    try:
        created_at_utc = parse_iso_utc_strict(created_at_str)
    except Exception:
        logger.error(
            "dispatch_sweeper: failed to parse created_at=%r for dispatch %s",
            created_at_str, meta.get("item_id", "?"), exc_info=True,
        )
        return None
    return now_utc - created_at_utc


def sweep_stale_dispatches(now_utc: Optional[datetime] = None) -> int:
    """Close in-flight dispatches that are past their timeout.

    Soft timeout: no active manager invocation and >10 minutes old.
    Hard timeout: >2 hours old regardless of invocation state.

    When a dispatch is closed, the acted_on source item is revived to
    ``actionable`` only if it is still in ``dispatched`` state. Items the
    user has since closed or suppressed are left alone (the state machine
    refuses those transitions).

    Returns the number of dispatches closed.
    """
    now = now_utc or datetime.now(timezone.utc)
    all_items = load_existing_dayflow_items(include_terminal=True)
    in_flight = _iter_in_flight_dispatches(all_items)
    if not in_flight:
        return 0

    active_names = _active_manager_names()
    closed = 0

    for item in in_flight:
        meta = get_meta(item)
        age = _dispatch_age(meta, now)
        if age is None:
            continue
        action_type = str(meta.get("action_type") or "").strip().lower()
        has_active_invocation = action_type in active_names

        if age > timedelta(hours=_DISPATCH_HARD_TIMEOUT_HOURS):
            stale_reason = f"exceeded hard timeout ({_DISPATCH_HARD_TIMEOUT_HOURS}h)"
        elif age > timedelta(minutes=_DISPATCH_SOFT_TIMEOUT_MINUTES) and not has_active_invocation:
            stale_reason = f"no active invocation after {_DISPATCH_SOFT_TIMEOUT_MINUTES}m"
        else:
            continue

        dispatch_item_id = str(meta.get("item_id") or item.get("id") or "").strip()
        if not dispatch_item_id:
            continue

        write_dayflow_item(
            dispatch_item_id,
            updates={
                "dispatch_status": "abandoned",
                "abandoned_at": now.isoformat(),
            },
            state="closed",
            reason=f"stale_dispatch: {stale_reason}",
            caller="dispatch_sweeper::sweep_stale_dispatches",
        )
        closed += 1

        acted_on_item_id = str(meta.get("acted_on_item_id") or "").strip()
        if acted_on_item_id:
            source_item = _find_item_by_id(all_items, acted_on_item_id)
            if source_item is not None:
                source_meta = get_meta(source_item)
                source_state = str(source_meta.get("state") or "").strip().lower()
                if source_state == "dispatched":
                    write_dayflow_item(
                        acted_on_item_id,
                        state="actionable",
                        reason="dispatch_abandoned",
                        caller="dispatch_sweeper::sweep_stale_dispatches",
                    )
                else:
                    logger.info(
                        "dispatch_sweeper: skipping revive of acted_on_item_id=%s "
                        "(source state=%r, not 'dispatched')",
                        acted_on_item_id, source_state,
                    )

        logger.info(
            "dispatch_sweeper: closed stale dispatch %s (%s) — %s",
            dispatch_item_id, action_type, stale_reason,
        )

    if closed:
        logger.info("dispatch_sweeper: auto-closed %d stale dispatch(es).", closed)
    return closed


def sweep_orphaned_dispatched_tasks(now_utc: Optional[datetime] = None) -> int:
    """Backstop sweep: close tasks stuck in ``state='dispatched'`` with no
    live dispatch row pointing at them.

    Closes the gap where the dispatch sweeper closed a dispatch cleanly
    but the source-task revive step crashed, OR a task was set to
    ``dispatched`` without a paired dispatch row at all. Without this,
    such tasks sit in ``dispatched`` forever — never reconsidered, never
    surfaced.

    A task is considered orphaned when:
      - state == 'dispatched'
      - dispatched_at is older than ``_TASK_DISPATCHED_TIMEOUT_HOURS``
      - no in-flight ``action_dispatch`` row has ``acted_on_item_id``
        equal to this task's id (otherwise the regular sweeper owns it)

    Such tasks are moved to ``closed`` (NOT ``actionable``) — re-promoting
    them would just have the orchestrator re-dispatch and re-stick. If
    the underlying need is still alive, the planner will create a fresh
    task next tick from current context.

    Returns the number of tasks closed.
    """
    now = now_utc or datetime.now(timezone.utc)
    all_items = load_existing_dayflow_items(include_terminal=True)
    in_flight = _iter_in_flight_dispatches(all_items)
    covered_task_ids = {
        str(get_meta(d).get("acted_on_item_id") or "").strip()
        for d in in_flight
    }
    covered_task_ids.discard("")

    cutoff = timedelta(hours=_TASK_DISPATCHED_TIMEOUT_HOURS)
    revived = 0

    for item in all_items:
        if not isinstance(item, dict):
            continue
        meta = get_meta(item)
        if str(meta.get("state") or "").strip().lower() != "dispatched":
            continue
        item_id = str(meta.get("item_id") or item.get("id") or "").strip()
        if not item_id or item_id in covered_task_ids:
            continue
        dispatched_at_str = str(meta.get("dispatched_at") or "").strip()
        if not dispatched_at_str:
            # No dispatched_at to age against — skip rather than guess.
            continue
        try:
            dispatched_at_utc = parse_iso_utc_strict(dispatched_at_str)
        except Exception:
            logger.error(
                "dispatch_sweeper: failed to parse dispatched_at=%r for task %s",
                dispatched_at_str, item_id, exc_info=True,
            )
            continue
        if (now - dispatched_at_utc) <= cutoff:
            continue

        write_dayflow_item(
            item_id,
            state="closed",
            reason=f"orphan_dispatched: stuck >{_TASK_DISPATCHED_TIMEOUT_HOURS}h with no live dispatch",
            caller="dispatch_sweeper::sweep_orphaned_dispatched_tasks",
        )
        revived += 1
        logger.info(
            "dispatch_sweeper: closed orphan-dispatched task %s (age=%s, summary=%r)",
            item_id, now - dispatched_at_utc, str(meta.get("summary") or "")[:80],
        )

    if revived:
        logger.info("dispatch_sweeper: closed %d orphan-dispatched task(s).", revived)
    return revived


def sweep_zombie_waiting_items(now_utc: Optional[datetime] = None) -> int:
    """Close ``state='waiting'`` items whose ``reactivate_at_utc`` is far past.

    Background: the relevance_cleaner closes stale waiting items per its
    "long-past reactivate, never picked up" rule, but it only sees items
    inside ``get_dayflow_items``'s 24h freshness window. Items that pass
    their reactivate time and don't get dispatched within ~24h drop out
    of the cleaner's view and become zombies — alive in DB, invisible to
    every agent, and (until ``ANCIENT_ITEM_OVERDUE_SECONDS`` was added)
    they kept the scheduler hot-looping on their stale timer.

    This sweep runs at tick start and closes any such items past
    ``_WAITING_ZOMBIE_OVERDUE_HOURS``. Threshold is set comfortably past
    the cleaner's effective window (cleaner sees up to 24h, runs every
    30m) so the cleaner gets first crack.

    Returns count closed.
    """
    now = now_utc or datetime.now(timezone.utc)
    all_items = load_existing_dayflow_items(include_terminal=True)
    cutoff = timedelta(hours=_WAITING_ZOMBIE_OVERDUE_HOURS)
    closed = 0

    for item in all_items:
        if not isinstance(item, dict):
            continue
        meta = get_meta(item)
        if str(meta.get("state") or "").strip().lower() != "waiting":
            continue
        item_id = str(meta.get("item_id") or item.get("id") or "").strip()
        if not item_id:
            continue
        raw = str(meta.get("reactivate_at_utc") or "").strip()
        if not raw:
            # No reactivate timer — the cleaner / state_mover own this case.
            continue
        try:
            reactivate_at_utc = parse_iso_utc_strict(raw)
        except Exception:
            logger.error(
                "dispatch_sweeper: failed to parse reactivate_at_utc=%r for waiting item %s",
                raw, item_id, exc_info=True,
            )
            continue
        overdue = now - reactivate_at_utc
        if overdue <= cutoff:
            continue

        write_dayflow_item(
            item_id,
            state="closed",
            reason=f"zombie_waiting: overdue {overdue.total_seconds() / 3600:.1f}h",
            caller="dispatch_sweeper::sweep_zombie_waiting_items",
        )
        closed += 1
        logger.info(
            "dispatch_sweeper: closed zombie waiting item %s (overdue=%s, summary=%r)",
            item_id, overdue, str(meta.get("summary") or "")[:80],
        )

    if closed:
        logger.info("dispatch_sweeper: closed %d zombie waiting item(s).", closed)
    return closed


# A dispatched work node's job is FROZEN when neither its subtree (a progressing worker writes
# checklist/evidence via the reconcile hook every planner turn) nor the job itself has shown activity
# for this long. Generous on purpose: one legitimately long tool call makes no writes.
_WORK_NODE_FROZEN_TIMEOUT_S = 20 * 60


def sweep_stuck_work_nodes(now_utc: Optional[datetime] = None) -> int:
    """Supervise in-flight work-node jobs (one thread per open task; see node_dispatch).

    Two dead states, both -> mark the node ``failed`` so work_repair adjudicates it
    (retry / escalate / abandon) on this same tick:

    - ORPHANED: the node is ``dispatched`` but no live job thread owns it — a process
      restart or a thread crash. Threads die with the process; the graph doesn't.
    - FROZEN: a job thread exists but neither the node's subtree nor the job has shown
      activity for ``_WORK_NODE_FROZEN_TIMEOUT_S``.

    The zombie thread (frozen case) is abandoned, not killed: its late ``done`` write is
    rejected by the transition machine (``failed -> done`` is illegal), so no torn state.
    Goal nodes are skipped — a goal sits ``dispatched`` by design while its work runs.

    Returns count failed.
    """
    from app.assistant.dayflow_orchestrator.node_dispatch import job_alive, job_started_at
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store

    now = now_utc or datetime.now(timezone.utc)
    store = get_dayflow_work_store()
    failed = 0

    for summary in store.list_work_objects():
        if str(summary.get("status") or "").lower() in ("done", "abandoned"):
            continue
        try:
            wo = store.load(summary["id"])
        except Exception:
            logger.error("dispatch_sweeper: work object %s not loadable during supervision",
                         summary.get("id"), exc_info=True)
            continue
        goal_id = wo.goal_node_id
        for node in wo.nodes.values():
            if node.id == goal_id or node.status != "dispatched":
                continue
            ref = f"{wo.id}::{node.id}"
            try:
                if not job_alive(ref):
                    reason = "orphaned (no live job thread — restart or thread crash)"
                elif _job_idle_seconds(wo, node, job_started_at(ref), now) > _WORK_NODE_FROZEN_TIMEOUT_S:
                    reason = f"frozen (no activity for {_WORK_NODE_FROZEN_TIMEOUT_S // 60}+ min)"
                else:
                    continue
                store.apply("set_status", {"work_id": wo.id, "node_id": node.id, "status": "failed"},
                            actor="dispatch_sweeper")
                failed += 1
                logger.error(
                    "dispatch_sweeper: work node %s marked failed — %s; work_repair adjudicates. (%r)",
                    ref, reason, (node.title or "")[:80],
                )
            except Exception:
                logger.error("dispatch_sweeper: supervision failed for %s", ref, exc_info=True)

    if failed:
        logger.info("dispatch_sweeper: failed %d stuck work node(s).", failed)
    return failed


def _job_idle_seconds(wo, node, started_at, now: datetime) -> float:
    """Seconds since the job last showed life: the newest updated_at across the node and its owned
    subtree (the worker's checklist/evidence writes), floored by the job's start time."""
    latest = started_at
    stack = [node.id]
    while stack:
        nid = stack.pop()
        n = wo.nodes.get(nid)
        if n is None:
            continue
        ts = getattr(n, "updated_at", None)
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if latest is None or ts > latest:
                latest = ts
        stack.extend(wo.children_of(nid))
    if latest is None:
        return float("inf")
    return (now - latest).total_seconds()


def list_active_dispatches() -> List[Dict[str, Any]]:
    """Return a compact list of in-flight dispatch rows.

    Read-only. Caller (view_materializer_node) uses this to filter
    out actionable items already covered by an in-flight dispatch.
    The list is sorted oldest-first and capped at the most recent 40.
    """
    all_items = load_existing_dayflow_items(include_terminal=True)
    in_flight = _iter_in_flight_dispatches(all_items)
    if not in_flight:
        return []

    rows: List[Dict[str, Any]] = []
    for item in in_flight:
        meta = get_meta(item)
        raw_task_id = str(meta.get("task_id") or "").strip()
        raw_acted_id = str(meta.get("acted_on_item_id") or "").strip()
        rows.append(
            {
                "dispatch_id": str(meta.get("dispatch_id") or "").strip(),
                "action_type": str(meta.get("action_type") or "").strip(),
                "task_id": long_to_short(raw_task_id, all_items),
                "plan_id": str(meta.get("plan_id") or "").strip(),
                "acted_on_item_id": long_to_short(raw_acted_id, all_items),
                "task_summary": str(meta.get("task_summary") or meta.get("summary") or "").strip(),
                "created_at": str(meta.get("created_at") or "").strip(),
                "time_local": str(meta.get("created_at_local") or "").strip(),
            }
        )

    rows.sort(key=lambda x: x.get("created_at", ""))
    return rows[-_DISPATCH_LIST_CAP:]
