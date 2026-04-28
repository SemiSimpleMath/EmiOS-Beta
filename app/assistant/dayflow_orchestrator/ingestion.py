"""Dayflow ingestion pre-step.

Scans all input sources for new data, deduplicates by existence,
persists as dayflow_items, and assigns short_ids. After this function
returns, ``get_dayflow_items()`` reflects the current state of the world.

This module replaces the ingestion logic previously scattered across
``blackboard_builder.py`` and ``input_message_builder.py``.
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
    _build_delegation_message,
    _build_ticket_message,
    _load_dayflow_requests,
    _load_emails_from_event_repo,
    mark_dayflow_requests_ingested,
)
from app.assistant.dayflow_orchestrator.orchestrator_status import (
    CHAT_WATERMARK_KEY,
    DAYFLOW_ORCHESTRATOR_ROOM_ID,
    load_orchestrator_status,
    persist_orchestrator_status,
)
from app.assistant.dayflow_orchestrator.state_store import (
    _load_latest_dayflow_item_map,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import parse_iso_utc_strict

logger = get_logger(__name__)

_SHORT_ID_RESET_THRESHOLD = 10_000


def _get_day_start_utc(now_utc: datetime) -> datetime:
    """Return the most recent day-reset boundary in UTC."""
    import os
    from app.assistant.utils.time_utils import get_local_timezone
    from datetime import timedelta

    raw = str(os.environ.get("DAY_RESET_HOUR") or "").strip()
    hour = int(raw) if raw and raw.lstrip("-").isdigit() else 5
    if hour < 0 or hour > 23:
        hour = 5

    local_tz = get_local_timezone()
    local_now = now_utc.astimezone(local_tz)
    reset_local = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if local_now < reset_local:
        reset_local = reset_local - timedelta(days=1)
    return reset_local.astimezone(timezone.utc)


def _load_chat_entitled_rooms() -> list[str]:
    """Read chat-ingestion room entitlements from dayflow access config."""
    access_path = (
        Path(__file__).resolve().parent.parent
        / "rooms"
        / DAYFLOW_ORCHESTRATOR_ROOM_ID
        / "access.json"
    )
    if not access_path.exists():
        raise FileNotFoundError(
            f"_load_chat_entitled_rooms: required file not found: {access_path}"
        )
    data = json.loads(access_path.read_text(encoding="utf-8"))
    ids = data.get("chat_ingestion_entitled_rooms")
    if ids is None:
        raise ValueError(
            "_load_chat_entitled_rooms: 'chat_ingestion_entitled_rooms' is required in access.json."
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
) -> list[Message]:
    """Ingest new cross-room chat as dayflow items."""
    entitled = _load_chat_entitled_rooms()

    day_start_utc = _get_day_start_utc(now_utc)
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

    # Advance watermark even if all messages were dupes — the source
    # rows have been seen and should not be re-queried next tick.
    if new_watermark is not None:
        status[CHAT_WATERMARK_KEY] = new_watermark.isoformat()
        persist_orchestrator_status(status)

    return new_only


def _ingest_emails(
    existing_ids: set[str], now_utc: datetime,
) -> list[Message]:
    """Ingest new emails from event repository as dayflow items."""
    email_events = _load_emails_from_event_repo(now_utc=now_utc)
    new: list[Message] = []
    for email_data in email_events:
        msg = _build_email_message(email_data=email_data, now_utc=now_utc)
        item_id = str(getattr(msg, "id", "") or "").strip()
        if item_id not in existing_ids:
            new.append(msg)
    return new


def _ingest_tickets(
    existing_ids: set[str], now_utc: datetime,
) -> list[Message]:
    """Ingest active dayflow tickets as dayflow items."""
    from app.assistant.ticket_manager import get_ticket_manager
    from app.assistant.ticket_manager.ticket import TicketState, TicketType

    active_states = [TicketState.PENDING, TicketState.PROPOSED, TicketState.SNOOZED]
    dayflow_types = [
        TicketType.DAYFLOW_ADVICE,
        TicketType.DAYFLOW_NOTIFY,
        TicketType.DAYFLOW_DECISION,
    ]
    tm = get_ticket_manager()
    new: list[Message] = []
    for tt in dayflow_types:
        tickets = tm.get_tickets(
            states=active_states,
            ticket_type=tt.value,
            limit=50,
            order_by="updated_at",
        )
        for ticket in tickets:
            msg = _build_ticket_message(ticket, now_utc)
            item_id = str(getattr(msg, "id", "") or "").strip()
            if item_id not in existing_ids:
                new.append(msg)
    return new


def _ingest_delegation_requests(
    existing_ids: set[str], now_utc: datetime,
) -> tuple[list[Message], list[Dict[str, Any]]]:
    """Ingest user delegation requests as dayflow items.

    Returns (new_messages, raw_requests) where raw_requests should be
    marked as ingested after successful persistence.
    """
    requests = _load_dayflow_requests(now_utc=now_utc)
    new: list[Message] = []
    ingested_requests: list[Dict[str, Any]] = []
    for req in requests:
        msg = _build_delegation_message(request=req, now_utc=now_utc)
        item_id = str(getattr(msg, "id", "") or "").strip()
        if item_id not in existing_ids:
            new.append(msg)
            ingested_requests.append(req)
    return new, ingested_requests


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
    existing_map = _load_latest_dayflow_item_map(include_terminal=True)
    existing_ids = set(existing_map.keys())

    # 2. Collect new items from each source.
    chat_items = _ingest_chat(existing_ids, now)
    email_items = _ingest_emails(existing_ids, now)
    ticket_items = _ingest_tickets(existing_ids, now)
    delegation_items, delegation_requests = _ingest_delegation_requests(existing_ids, now)

    all_new = chat_items + email_items + ticket_items + delegation_items

    if not all_new:
        logger.info("run_dayflow_ingestion: no new items to ingest.")
        return {
            "chat": 0, "email": 0, "ticket": 0, "delegation": 0, "total": 0,
        }

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
    if counter_start >= _SHORT_ID_RESET_THRESHOLD:
        counter_start = 1

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

    # 5. Mark delegation requests as ingested.
    if delegation_requests:
        mark_dayflow_requests_ingested(delegation_requests)

    summary = {
        "chat": len(chat_items),
        "email": len(email_items),
        "ticket": len(ticket_items),
        "delegation": len(delegation_items),
        "total": len(all_new),
    }
    logger.info(
        "run_dayflow_ingestion: ingested %d item(s) — chat=%d email=%d ticket=%d delegation=%d",
        summary["total"], summary["chat"], summary["email"],
        summary["ticket"], summary["delegation"],
    )
    return summary
