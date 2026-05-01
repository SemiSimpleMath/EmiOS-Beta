"""Single source of truth for all dayflow item writes.

Every create, update, state transition, and metadata patch goes through
``write_dayflow_item``.  No other code path should write to
unified_log_2026 with source='dayflow_item' directly.

Design principles:
- Create and update are the same function.  If the item_id exists in
  the DB, read-merge-write.  If it doesn't, insert a new row.
- State transitions are validated against ALLOWED_TRANSITIONS.
- Every write stamps ``last_reviewed_at``.
- Every write logs the caller, item_id, and what changed.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.sql import select, insert

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

DAYFLOW_ITEM_SOURCE = "dayflow_item"
DAYFLOW_ROOM_ID = "dayflow_orchestrator"

# ── State machine ────────────────────────────────────────────────
# Maps current_state → set of allowed next states.
# Any transition not listed here is rejected.
ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    "new":                frozenset({"artifact", "needs_planning", "important_open", "actionable", "suppressed"}),
    "artifact":           frozenset({"needs_planning", "important_open", "actionable", "watching", "closed", "suppressed", "pending_directive"}),
    "needs_planning":     frozenset({"important_open", "actionable", "waiting", "closed", "suppressed"}),
    "important_open":     frozenset({"actionable", "waiting", "watching", "dispatched", "closed", "suppressed", "pending_directive"}),
    "actionable":         frozenset({"dispatched", "waiting", "watching", "closed", "suppressed", "pending_directive"}),
    "dispatched":         frozenset({"closed", "waiting", "actionable", "pending_directive"}),
    "waiting":            frozenset({"actionable", "dispatched", "closed", "suppressed"}),
    "watching":           frozenset({"actionable", "important_open", "closed", "suppressed"}),
    "closed":             frozenset({"suppressed", "actionable", "pending_directive"}),  # reopen allowed; user-directive can revive
    "suppressed":         frozenset({"pending_directive"}),  # narrow exception: a user directive references this artifact
    # User responded to a ticket with a follow-up directive in user_text. The
    # planner owes a decision: dispatch, ignore, or escalate. Closes via the
    # normal dispatched→closed path or directly to closed/suppressed if the
    # planner determines no action is needed.
    "pending_directive":  frozenset({"actionable", "dispatched", "closed", "suppressed"}),
    # Plan synopsis states:
    "active":             frozenset({"closed", "suppressed"}),
}

# States that are terminal or resolved.
DONE_STATES = frozenset({"closed"})
TERMINAL_STATES = frozenset({"suppressed"})
RESOLVED_STATES = DONE_STATES | TERMINAL_STATES


def _read_metadata(row: UnifiedLog2026) -> Dict[str, Any]:
    """Extract metadata dict from a DB row, handling double-encoding."""
    raw = row.metadata_json
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        # Unwrap double-encoded JSON.
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def short_to_long_id(short_id: int | str) -> Optional[str]:
    """Look up the most recent dayflow item with this short_id.

    Returns the canonical item_id, or None if not found.
    This is a direct DB query — no cache, no stale data.
    """
    short_id_val = str(short_id).strip()
    if not short_id_val:
        return None

    with get_db_manager().read_session() as session:
        stmt = (
            select(UnifiedLog2026)
            .where(UnifiedLog2026.source == DAYFLOW_ITEM_SOURCE)
            .where(UnifiedLog2026.room_id == DAYFLOW_ROOM_ID)
            .order_by(UnifiedLog2026.timestamp.desc())
        )
        rows = session.execute(stmt).scalars().all()

        for row in rows:
            meta = _read_metadata(row)
            row_sid = str(meta.get("short_id") or "").strip()
            if row_sid == short_id_val:
                return str(meta.get("item_id") or row.id or "").strip()
        return None


def get_max_short_id() -> int:
    """Return the highest numeric short_id currently in the DB."""
    with get_db_manager().read_session() as session:
        stmt = (
            select(UnifiedLog2026)
            .where(UnifiedLog2026.source == DAYFLOW_ITEM_SOURCE)
            .where(UnifiedLog2026.room_id == DAYFLOW_ROOM_ID)
        )
        rows = session.execute(stmt).scalars().all()

        max_id = 0
        for row in rows:
            meta = _read_metadata(row)
            sid = meta.get("short_id")
            if sid is not None:
                try:
                    val = int(sid)
                    if val > max_id:
                        max_id = val
                except (ValueError, TypeError):
                    pass
        return max_id


def resolve_short_id(raw_id: str) -> str:
    """Resolve a short_id or item_id to a canonical item_id.

    If raw_id looks like a short numeric id, look it up.
    If it looks like a full item_id (contains ':'), return as-is.
    Returns the resolved id, or the input unchanged if lookup fails
    (with a warning log).
    """
    raw_id = str(raw_id).strip()
    if not raw_id:
        return raw_id

    # If it contains a colon, it's already a canonical item_id.
    if ":" in raw_id:
        return raw_id

    # Try numeric short_id lookup.
    resolved = short_to_long_id(raw_id)
    if resolved:
        return resolved

    logger.warning("resolve_short_id: short_id '%s' not found in DB", raw_id)
    return raw_id


def write_dayflow_item(
    item_id: str,
    *,
    updates: Optional[Dict[str, Any]] = None,
    state: Optional[str] = None,
    reason: str = "",
    caller: str = "",
    # Fields for new item creation (ignored if item already exists):
    content: str = "",
    source_type: str = "",
    sender: str = "",
    sub_data_type: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create or update a dayflow item.  Single entry point for all writes.

    For **existing items**: reads current metadata from DB, merges
    ``updates`` dict, validates state transition if ``state`` is given,
    and writes back.  Only touched keys change.

    For **new items**: creates a row with the given metadata.
    ``updates`` must contain at least ``source_type`` and ``summary``.

    Returns the final merged metadata dict.

    Raises:
        ValueError: on invalid state transition, missing required fields,
                    or other contract violations.
    """
    if not item_id or not str(item_id).strip():
        raise ValueError(f"write_dayflow_item: item_id is required (caller={caller})")
    item_id = str(item_id).strip()

    now_utc = datetime.now(timezone.utc)
    updates = dict(updates or {})

    # Always stamp the review time.
    updates["last_reviewed_at"] = now_utc.isoformat()

    with get_db_manager().transaction(op=f"dayflow.write_item:{caller}") as session:
        row = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.id == item_id)
            .where(UnifiedLog2026.source == DAYFLOW_ITEM_SOURCE)
        ).scalar_one_or_none()

        if row is not None:
            # ── UPDATE existing item ─────────────────────────────
            meta = _read_metadata(row)

            # Validate state transition if requested.
            if state is not None:
                current_state = str(meta.get("state") or "").strip().lower()
                _validate_transition(item_id, current_state, state, caller)
                updates["state"] = state
                updates["state_reason"] = reason or f"{caller}:{current_state}_to_{state}"

            meta.update(updates)

            from sqlalchemy import update as sql_update
            session.execute(
                sql_update(UnifiedLog2026)
                .where(UnifiedLog2026.id == item_id)
                .values(
                    metadata_json=meta,
                    message=str(meta.get("summary") or meta.get("content") or row.message or ""),
                    timestamp=now_utc,
                )
            )

            logger.info(
                "write_dayflow_item UPDATE | item=%s | state=%s | keys=%s | caller=%s",
                item_id,
                meta.get("state", ""),
                sorted(updates.keys()),
                caller,
            )
            return meta

        else:
            # ── CREATE new item ──────────────────────────────────
            if state is not None:
                updates["state"] = state
                updates.setdefault("state_reason", reason or f"{caller}:created")

            updates.setdefault("item_id", item_id)
            updates.setdefault("created_at", now_utc.isoformat())
            updates.setdefault("data_type", "dayflow_input_item")
            updates.setdefault("source_type", source_type)

            # Validate required fields for new items.
            if not updates.get("source_type"):
                raise ValueError(
                    f"write_dayflow_item: new item '{item_id}' requires source_type (caller={caller})"
                )
            if not updates.get("summary") and not content:
                raise ValueError(
                    f"write_dayflow_item: new item '{item_id}' requires summary or content (caller={caller})"
                )
            if not updates.get("summary") and content:
                updates["summary"] = content

            meta = dict(updates)

            stmt = insert(UnifiedLog2026).values(
                id=item_id,
                timestamp=now_utc,
                role="system",
                message=str(meta.get("summary") or content or ""),
                source=DAYFLOW_ITEM_SOURCE,
                processed=False,
                room_id=DAYFLOW_ROOM_ID,
                metadata_json=meta,
                data_json={"data_type": meta.get("data_type", "dayflow_input_item")},
                content_type="text",
            )
            session.execute(stmt)

            logger.info(
                "write_dayflow_item CREATE | item=%s | state=%s | source_type=%s | caller=%s",
                item_id,
                meta.get("state", ""),
                meta.get("source_type", ""),
                caller,
            )
            return meta


def _validate_transition(
    item_id: str,
    current_state: str,
    new_state: str,
    caller: str,
) -> None:
    """Raise ValueError if the state transition is not allowed."""
    new_state = new_state.strip().lower()
    current_state = current_state.strip().lower()

    if not current_state:
        # Item has no state yet (legacy/corrupt) — allow any transition.
        logger.warning(
            "write_dayflow_item: item '%s' has no current state, allowing transition to '%s' (caller=%s)",
            item_id, new_state, caller,
        )
        return

    allowed = ALLOWED_TRANSITIONS.get(current_state)
    if allowed is None:
        raise ValueError(
            f"write_dayflow_item: unknown current state '{current_state}' "
            f"for item '{item_id}' (caller={caller})"
        )
    if new_state not in allowed:
        raise ValueError(
            f"write_dayflow_item: invalid transition '{current_state}' → '{new_state}' "
            f"for item '{item_id}' (caller={caller}). "
            f"Allowed: {sorted(allowed)}"
        )


def write_dayflow_items_batch(
    items: List[Dict[str, Any]],
    *,
    caller: str = "",
) -> int:
    """Create multiple new dayflow items in a single transaction.

    Each dict in ``items`` must have at least ``item_id``, ``source_type``,
    and ``summary``.  State defaults to the value in the dict or
    ``important_open`` if not specified.

    Returns the count of items written.
    """
    if not items:
        return 0

    now_utc = datetime.now(timezone.utc)
    records = []

    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"write_dayflow_items_batch: expected dict, got {type(item).__name__}")

        item_id = str(item.get("item_id") or "").strip()
        if not item_id:
            raise ValueError(f"write_dayflow_items_batch: item missing item_id (caller={caller})")

        meta = dict(item)
        meta.setdefault("created_at", now_utc.isoformat())
        meta.setdefault("last_reviewed_at", now_utc.isoformat())
        meta.setdefault("data_type", "dayflow_input_item")
        meta.setdefault("state", "important_open")

        if not meta.get("source_type"):
            raise ValueError(
                f"write_dayflow_items_batch: item '{item_id}' missing source_type (caller={caller})"
            )
        if not meta.get("summary"):
            raise ValueError(
                f"write_dayflow_items_batch: item '{item_id}' missing summary (caller={caller})"
            )

        records.append({
            "id": item_id,
            "timestamp": now_utc,
            "role": "system",
            "message": str(meta.get("summary") or ""),
            "source": DAYFLOW_ITEM_SOURCE,
            "processed": False,
            "room_id": DAYFLOW_ROOM_ID,
            "metadata_json": meta,
            "data_json": {"data_type": meta.get("data_type", "dayflow_input_item")},
            "content_type": "text",
        })

    with get_db_manager().transaction(op=f"dayflow.write_items_batch:{caller}") as session:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        stmt = sqlite_insert(UnifiedLog2026).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "timestamp": stmt.excluded.timestamp,
                "message": stmt.excluded.message,
                "metadata_json": stmt.excluded.metadata_json,
                "data_json": stmt.excluded.data_json,
            },
        )
        session.execute(stmt)

    logger.info(
        "write_dayflow_items_batch: wrote %d item(s) (caller=%s)",
        len(records), caller,
    )
    return len(records)
