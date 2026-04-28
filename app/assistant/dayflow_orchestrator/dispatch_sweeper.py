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
   list of in-flight dispatch rows for the wait_interrupt_promoter node.

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
_DISPATCH_HARD_TIMEOUT_HOURS: int = 4
_DISPATCH_LIST_CAP: int = 40


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
    Hard timeout: >4 hours old regardless of invocation state.

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


def list_active_dispatches() -> List[Dict[str, Any]]:
    """Return a compact list of in-flight dispatch rows.

    Read-only. Callers (wait_interrupt_promoter_node) use this to filter
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
