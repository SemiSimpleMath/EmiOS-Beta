"""Dayflow ingestion pre-step.

Scans all input sources for new data, deduplicates by existence,
persists as dayflow_items, and assigns short_ids. After this function
returns, ``get_dayflow_items()`` reflects the current state of the world.

This module replaces the ingestion logic previously scattered across
``blackboard_builder.py`` and ``input_message_builder.py``.

Since 2026-09-30 chat and email items are wake context only (state_mover matches them against work
waiting on a reply); the steward's intake comes from the brain's concern handoffs.
The user-delegation lane is retired: the brain reads the user's own message. Pod intake is retired
(2026-10-05): the camera frames it took now reach the brain's inbox (subconscious/brain_inbox.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from app.assistant.dayflow_orchestrator.chat_ingestion import ingest_cross_room_chat
from app.assistant.dayflow_orchestrator.contracts import assign_short_ids, get_meta
from app.assistant.dayflow_orchestrator.input_message_builder import (
    _build_email_message,
    _load_todays_emails,
)
from app.assistant.dayflow_orchestrator.orchestrator_status import (
    CHAT_WATERMARK_KEY,
    DAYFLOW_ORCHESTRATOR_ROOM_ID,
    load_orchestrator_status,
    persist_orchestrator_status,
)
from app.assistant.dayflow_orchestrator.state_store import (
    load_ingestion_identity_index,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import day_reset_cutoff_utc, parse_iso_utc_strict

logger = get_logger(__name__)




def _load_chat_entitled_rooms() -> list[str]:
    """Read chat-ingestion room entitlements from dayflow access config."""
    from app.assistant.rooms.room_resource_loader import load_room_access
    data = load_room_access(DAYFLOW_ORCHESTRATOR_ROOM_ID)
    ids = data.get("chat_ingestion_entitled_rooms")
    if ids is None:
        raise ValueError(
            "_load_chat_entitled_rooms: 'chat_ingestion_entitled_rooms' is required in the dayflow_orchestrator room's access block."
        )
    if not isinstance(ids, list):
        raise ValueError(
            "_load_chat_entitled_rooms: 'chat_ingestion_entitled_rooms' must be a list."
        )
    normalized = [str(r).strip() for r in ids if isinstance(r, str) and r.strip()]
    if not normalized:
        raise ValueError(
            "_load_chat_entitled_rooms: 'chat_ingestion_entitled_rooms' must include at least one room."
        )
    return normalized


def _ingest_chat(
    existing_ids: set[str], now_utc: datetime,
) -> tuple[list[Message], datetime | None]:
    """Ingest new cross-room chat as dayflow items."""
    entitled = _load_chat_entitled_rooms()

    day_start_utc = day_reset_cutoff_utc(now_utc)
    status = load_orchestrator_status()
    raw_watermark = status.get(CHAT_WATERMARK_KEY)
    since_utc = day_start_utc
    if raw_watermark:
        watermark = parse_iso_utc_strict(raw_watermark, label=CHAT_WATERMARK_KEY)
        since_utc = max(day_start_utc, watermark)

    messages, new_watermark = ingest_cross_room_chat(
        since_utc=since_utc,
        entitled_room_ids=entitled,
        now_utc=now_utc,
    )

    new_only = [
        m for m in messages
        if str(getattr(m, "id", "") or "").strip() not in existing_ids
    ]

    return new_only, new_watermark


def _ingest_emails(
    existing_ids: set[str], now_utc: datetime,
) -> list[Message]:
    """Ingest today's new important emails (email pods) as dayflow items: wake context for work
    waiting on a reply (state_mover), like chat items. Since 2026-09-30 they are not intake for the
    steward: the brain reads every email and hands what needs doing over as a concern."""
    email_events = _load_todays_emails(now_utc=now_utc)
    new: list[Message] = []
    for email_data in email_events:
        msg = _build_email_message(email_data=email_data, now_utc=now_utc)
        item_id = str(getattr(msg, "id", "") or "").strip()
        if item_id not in existing_ids:
            new.append(msg)
    return new


def run_dayflow_ingestion(
    *, now_utc: datetime | None = None,
) -> Dict[str, Any]:
    """Run the dayflow ingestion pre-step.

    Scans all input sources, deduplicates against existing items,
    assigns short_ids, and persists new items. After this returns,
    ``get_dayflow_items()`` is up to date.

    Returns a summary dict with counts per source.
    """
    now = now_utc or datetime.now(timezone.utc)

    # 1. Load existing item IDs for deduplication.
    existing_map = load_ingestion_identity_index()
    existing_ids = set(existing_map.keys())

    # 2. Collect new items from each source.
    chat_items, chat_watermark = _ingest_chat(existing_ids, now)
    email_items = _ingest_emails(existing_ids, now)

    all_new = chat_items + email_items

    if not all_new:
        logger.info("run_dayflow_ingestion: no new items to ingest.")

    # 3. Assign short_ids.
    #    Find max existing short_id across all items (including terminal).
    all_existing_items = list(existing_map.values())
    max_short_id = 0
    for item in all_existing_items:
        meta = get_meta(item)
        raw_sid = meta.get("short_id")
        if raw_sid is not None:
            try:
                val = int(raw_sid)
                if val > max_short_id:
                    max_short_id = val
            except (ValueError, TypeError):
                pass

    counter_start = max_short_id + 1

    # Convert Messages to dicts for assign_short_ids.
    new_dicts = []
    for msg in all_new:
        if hasattr(msg, "model_dump"):
            new_dicts.append(msg.model_dump(mode="json"))
        else:
            new_dicts.append(msg.dict())

    assign_short_ids(new_dicts, counter_start=counter_start)

    # Copy short_ids back onto Message metadata for persistence.
    for msg, d in zip(all_new, new_dicts):
        meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
        short_id = meta.get("short_id")
        if short_id is not None and hasattr(msg, "metadata") and isinstance(msg.metadata, dict):
            msg.metadata["short_id"] = short_id

    # 4. Persist all new items.
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
    batch = []
    for msg in all_new:
        meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else {}
        meta.setdefault("item_id", msg.id)
        meta.setdefault("summary", msg.content or "")
        meta.setdefault("source_type", meta.get("source_type", ""))
        batch.append(meta)
    if batch:
        write_dayflow_items_batch(batch, caller="dayflow_ingestion")

    # Commit cursors only after destination persistence and source acknowledgement.
    cursor_changes = {}
    if chat_watermark is not None:
        cursor_changes[CHAT_WATERMARK_KEY] = chat_watermark.isoformat()
    if cursor_changes:
        persist_orchestrator_status(cursor_changes)

    summary = {
        "chat": len(chat_items),
        "email": len(email_items),
        "total": len(all_new),
    }
    logger.info(
        "run_dayflow_ingestion: ingested %d item(s). chat=%d email=%d",
        summary["total"], summary["chat"], summary["email"],
    )
    return summary
