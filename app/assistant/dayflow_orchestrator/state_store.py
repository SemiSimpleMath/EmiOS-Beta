"""Dayflow orchestrator persistent state store.

All dayflow items are Messages stored in unified_log_2026 with source='dayflow_item'.
State lives in metadata_json. Upsert keyed on Message.id (= metadata.item_id).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.sql import select

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

from app.assistant.dayflow_orchestrator.dayflow_item_writer import (
    DAYFLOW_ITEM_SOURCE,
    DAYFLOW_ROOM_ID,
    DONE_STATES,
    RESOLVED_STATES,
    TERMINAL_STATES,
)


def _row_to_dict(row: UnifiedLog2026) -> Dict[str, Any]:
    """Convert a UnifiedLog2026 ORM row into a serialized-Message-shaped dict."""
    import json as _json
    raw_meta = row.metadata_json
    if isinstance(raw_meta, dict):
        metadata = raw_meta
    elif isinstance(raw_meta, str):
        try:
            metadata = _json.loads(raw_meta)
        except (_json.JSONDecodeError, TypeError):
            metadata = {}
    else:
        metadata = {}
    # Guard against double-encoded JSON (string-of-JSON stored by a
    # buggy writer).  Unwrap one extra layer so the item is not silently
    # discarded.
    if isinstance(metadata, str):
        try:
            metadata = _json.loads(metadata)
        except (_json.JSONDecodeError, TypeError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    ts = row.timestamp
    return {
        "id": row.id,
        "data_type": metadata.get("data_type", "dayflow_input_item"),
        "sub_data_type": metadata.get("sub_data_type", []),
        "sender": row.speaker_name or metadata.get("sender", ""),
        "content": row.message or "",
        "timestamp": ts.isoformat() if isinstance(ts, datetime) else str(ts or ""),
        "room_id": row.room_id,
        "metadata": metadata,
        "data": row.data_json if isinstance(row.data_json, dict) else {},
    }


def _effective_exclude_states(
    *,
    include_terminal: bool,
    exclude_states: frozenset[str] | None,
) -> frozenset[str]:
    return (
        exclude_states if exclude_states is not None
        else (frozenset() if include_terminal else TERMINAL_STATES)
    )


def _load_latest_dayflow_item_map(
    *,
    include_terminal: bool = False,
    exclude_states: frozenset[str] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Load latest dayflow item snapshot keyed by logical item_id.

    This is the canonical latest-version resolver and should be used by all
    callers that need current item state.
    """
    effective_exclude = _effective_exclude_states(
        include_terminal=include_terminal,
        exclude_states=exclude_states,
    )

    try:
        with get_db_manager().read_session() as session:
            stmt = (
                select(UnifiedLog2026)
                .where(UnifiedLog2026.source == DAYFLOW_ITEM_SOURCE)
                .where(UnifiedLog2026.room_id == DAYFLOW_ROOM_ID)
                .order_by(UnifiedLog2026.timestamp.asc())
            )
            rows = session.execute(stmt).scalars().all()

            latest_by_item_id: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                item = _row_to_dict(row)
                meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                logical_id = str(meta.get("item_id") or item.get("id") or "").strip()
                if not logical_id:
                    continue
                latest_by_item_id[logical_id] = item

            if effective_exclude:
                filtered: Dict[str, Dict[str, Any]] = {}
                for logical_id, item in latest_by_item_id.items():
                    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                    state = str(meta.get("state") or "").strip().lower()
                    if state in effective_exclude:
                        continue
                    filtered[logical_id] = item
                latest_by_item_id = filtered

            logger.info(
                "_load_latest_dayflow_item_map: loaded %d current item(s) (exclude_states=%s, raw_rows=%d)",
                len(latest_by_item_id),
                effective_exclude or "none",
                len(rows),
            )
            return latest_by_item_id
    except Exception as e:
        logger.error("_load_latest_dayflow_item_map: failed: %s", e)
        logger.debug("_load_latest_dayflow_item_map exception details", exc_info=True)
        raise


_MAX_AGE_HOURS = 24
_CLOSED_MAX_AGE_HOURS = 2


def get_dayflow_items() -> List[Dict[str, Any]]:
    """Return all dayflow items eligible for the orchestrator right now.

    Single source of truth for orchestrator agents. Excludes:
    - Suppressed items (terminal — permanently invisible).
    - Active items older than 24 hours (plans are same-day).
    - Closed items older than 2 hours (recent context only).

    Presentation concerns (local times, relative durations like "45m ago")
    belong in the prompt rendering layer, not here.
    """
    from app.assistant.utils.time_utils import parse_iso_utc

    now_utc = datetime.now(timezone.utc)
    active_cutoff = now_utc.timestamp() - (_MAX_AGE_HOURS * 3600)
    closed_cutoff = now_utc.timestamp() - (_CLOSED_MAX_AGE_HOURS * 3600)

    item_map = _load_latest_dayflow_item_map(
        include_terminal=False,
        exclude_states=TERMINAL_STATES,
    )

    items: List[Dict[str, Any]] = []
    for item in item_map.values():
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        state = str(meta.get("state") or "").strip().lower()
        is_closed = state in DONE_STATES

        # Use last_reviewed_at as the freshness indicator — it's updated on
        # every state change. Fall back to created_at, then timestamp.
        raw_ts = (
            meta.get("last_reviewed_at")
            or meta.get("created_at")
            or str(item.get("timestamp") or "")
        )
        if isinstance(raw_ts, str) and raw_ts.strip():
            parsed = parse_iso_utc(raw_ts)
            if parsed is not None:
                cutoff = closed_cutoff if is_closed else active_cutoff
                if parsed.timestamp() < cutoff:
                    continue
            else:
                # Unparseable timestamp — exclude to avoid showing ancient items.
                logger.warning("get_dayflow_items: unparseable timestamp for item '%s', excluding.", meta.get("item_id", ""))
                continue
        items.append(item)

    # Sort by parsed timestamp (datetime-safe), not string comparison.
    def _sort_key(it):
        ts = it.get("timestamp") or ""
        parsed = parse_iso_utc(str(ts)) if isinstance(ts, str) else None
        return parsed.timestamp() if parsed else 0.0
    items.sort(key=_sort_key)
    return items


def load_existing_dayflow_items(
    *,
    include_terminal: bool = False,
    exclude_states: frozenset[str] | None = None,
) -> List[Dict[str, Any]]:
    """
    Load dayflow items from unified_log_2026.

    Returns list of dicts matching the serialized Message shape.

    Filtering priority:
    1. If *exclude_states* is given it takes precedence over *include_terminal*.
    2. Otherwise, *include_terminal=False* (default) excludes TERMINAL_STATES.
    """
    latest_by_item_id = _load_latest_dayflow_item_map(
        include_terminal=include_terminal,
        exclude_states=exclude_states,
    )
    items = list(latest_by_item_id.values())
    items.sort(key=lambda it: str(it.get("timestamp") or ""))
    return items


def get_latest_dayflow_item_by_id(
    item_id: str,
    *,
    include_terminal: bool = True,
    exclude_states: frozenset[str] | None = None,
) -> Optional[Dict[str, Any]]:
    """
    Get the current/latest snapshot for one logical dayflow item_id.

    Returns None when the item does not exist (or is filtered out).
    """
    logical_id = str(item_id or "").strip()
    if not logical_id:
        raise ValueError("get_latest_dayflow_item_by_id: item_id must be non-empty.")
    latest_by_item_id = _load_latest_dayflow_item_map(
        include_terminal=include_terminal,
        exclude_states=exclude_states,
    )
    return latest_by_item_id.get(logical_id)


def get_latest_dayflow_items_by_ids(
    item_ids: Iterable[str],
    *,
    include_terminal: bool = True,
    exclude_states: frozenset[str] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Get latest snapshots for a batch of logical dayflow item_ids.

    Returns a dict keyed by item_id for found/current items only.
    """
    wanted = {str(v or "").strip() for v in item_ids}
    wanted.discard("")
    if not wanted:
        return {}
    latest_by_item_id = _load_latest_dayflow_item_map(
        include_terminal=include_terminal,
        exclude_states=exclude_states,
    )
    return {
        logical_id: item
        for logical_id, item in latest_by_item_id.items()
        if logical_id in wanted
    }


def build_plan_synopsis_dicts(plan_synopses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build plan synopsis metadata dicts suitable for ``write_dayflow_items_batch``.

    Each returned dict has all metadata fields plus ``item_id``, ``source_type``,
    and ``summary`` required by the batch writer.
    """
    if not plan_synopses:
        return []

    now_utc = datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []

    for idx, raw in enumerate(plan_synopses):
        if not isinstance(raw, dict):
            raise ValueError(f"build_plan_synopsis_dicts: entry {idx} must be a dict.")

        plan_id = str(raw.get("plan_id") or "").strip()
        if not plan_id:
            raise ValueError(f"build_plan_synopsis_dicts: entry {idx} missing plan_id.")
        objective = str(raw.get("objective") or "").strip()
        synopsis = str(raw.get("synopsis") or "").strip()
        if not objective or not synopsis:
            raise ValueError(
                f"build_plan_synopsis_dicts: entry {idx} requires objective and synopsis."
            )

        success_criteria = str(raw.get("success_criteria") or "").strip()
        step_outline_raw = raw.get("step_outline")
        if isinstance(step_outline_raw, list):
            step_outline = [str(v).strip() for v in step_outline_raw if str(v).strip()]
        else:
            step_outline = []

        item_id = f"plan_synopsis:{plan_id}"
        summary = f"{plan_id}: {objective}"
        metadata: Dict[str, Any] = {
            "item_id": item_id,
            "plan_id": plan_id,
            "source_type": "plan_synopsis",
            "event_type": "plan_overview",
            "created_at": now_utc.isoformat(),
            "summary": summary,
            "objective": objective,
            "synopsis": synopsis,
            "success_criteria": success_criteria,
            "step_outline": step_outline,
            "importance": "medium",
            "actionability": "context_only",
            "state": "active",
            "state_reason": "planner_synopsis",
            "last_reviewed_at": now_utc.isoformat(),
            "cooldown_until": None,
            "linked_item_ids": [],
        }
        results.append(metadata)

    return results


def build_plan_synopsis_messages(plan_synopses: List[Dict[str, Any]]) -> List[Message]:
    """
    Persist planner-authored plan synopsis guidance as context-only dayflow items.

    Delegates to ``build_plan_synopsis_dicts`` and wraps as Message objects.
    """
    dicts = build_plan_synopsis_dicts(plan_synopses)
    if not dicts:
        return []

    now_utc = datetime.now(timezone.utc)
    messages: List[Message] = []
    for metadata in dicts:
        msg = Message(
            id=str(metadata["item_id"]),
            data_type="dayflow_input_item",
            sub_data_type=["dayflow_orchestrator", "plan_synopsis", "dayflow_synopsis"],
            sender="strategic_planner",
            content=str(metadata.get("summary") or ""),
            timestamp=now_utc,
            room_id=DAYFLOW_ROOM_ID,
            metadata=metadata,
        )
        messages.append(msg)

    return messages


def write_action_log(
    *,
    task_id: str,
    plan_id: str = "",
    event_type: str,
    summary: str,
    detail: str = "",
    idempotency_key: str = "",
) -> None:
    """Write a single action log message to the dayflow DB.

    event_type: 'dispatch', 'result', 'ticket_sent', 'ticket_response'

    idempotency_key: if provided, used as the seed for the item_id so
    repeated calls with the same key upsert rather than creating duplicates.
    If empty, a timestamp-based key is used (each call creates a new entry).

    Note: action logs are stored with state='closed' so they are
    automatically pruned from get_dayflow_items() after _CLOSED_MAX_AGE_HOURS
    (currently 2h). This is intentional — they are ephemeral context for the
    current planning window, not permanent records.
    """
    import hashlib
    from app.assistant.utils.time_utils import get_local_timezone

    now_utc = datetime.now(timezone.utc)
    local_tz = get_local_timezone()
    time_local = now_utc.astimezone(local_tz).strftime("%I:%M %p")

    seed = idempotency_key or f"{task_id}|{event_type}|{now_utc.isoformat()}"
    item_id = f"action_log:{hashlib.sha256(seed.encode()).hexdigest()[:16]}"

    meta = {
        "item_id": item_id,
        "source_type": "action_log",
        "event_type": event_type,
        "task_id": task_id,
        "plan_id": plan_id,
        "summary": summary,
        "detail": detail,
        "time_local": time_local,
        "created_at": now_utc.isoformat(),
        "state": "closed",
        "data_type": "dayflow_input_item",
    }

    from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
    write_dayflow_item(
        item_id,
        updates=meta,
        state="closed",
        reason="action_log",
        caller="write_action_log",
        content=summary,
        source_type="action_log",
        sender="dayflow_orchestrator",
        sub_data_type=["dayflow_orchestrator", "action_log"],
    )
    logger.info("write_action_log: %s | %s | %s", event_type, task_id, summary[:80])


def reload_active_items_onto_blackboard(blackboard, *, now_utc: datetime, caller: str = "") -> int:
    """Reload active_dayflow_items and existing_dayflow_items from DB onto blackboard.

    Shared by triage_persist_node, state_mover_persist_node, and
    planner_persist_node — all need the same reload after persisting.

    Returns count of active items loaded.
    """
    from app.assistant.dayflow_orchestrator.contracts import get_meta
    from app.assistant.dayflow_orchestrator.blackboard_builder import enrich_items_with_local_times

    active_items = load_existing_dayflow_items(include_terminal=True)
    active_items = [
        item for item in active_items
        if str(get_meta(item).get("source_type") or "").strip().lower() != "chat"
    ]
    enrich_items_with_local_times(active_items, now_utc)

    blackboard.update_state_value("active_dayflow_items", active_items)
    blackboard.update_state_value("existing_dayflow_items", load_existing_dayflow_items(include_terminal=True))
    logger.info(
        "reload_active_items_onto_blackboard: %d active item(s) [caller=%s].",
        len(active_items),
        caller or "unknown",
    )
    return len(active_items)
