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
    _build_pod_message,
    _load_dayflow_requests,
    _load_emails_from_event_repo,
    mark_dayflow_requests_ingested,
)
from app.assistant.dayflow_orchestrator.orchestrator_status import (
    CHAT_WATERMARK_KEY,
    DAYFLOW_ORCHESTRATOR_ROOM_ID,
    POD_WATERMARK_KEY,
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


def _load_dayflow_pod_kinds_filter() -> list[Dict[str, Any]]:
    """Read pod-kind allowlist from dayflow access config.

    Each entry is a dict with at least a 'kind' field; an optional
    'source_kind' narrows further. Empty list means no pod ingestion.
    """
    access_path = (
        Path(__file__).resolve().parent.parent
        / "rooms"
        / DAYFLOW_ORCHESTRATOR_ROOM_ID
        / "access.json"
    )
    if not access_path.exists():
        return []
    data = json.loads(access_path.read_text(encoding="utf-8"))
    raw = data.get("ingestion_pod_kinds")
    if raw is None:
        return []  # absent = pod ingestion off
    if not isinstance(raw, list):
        raise ValueError(
            "_load_dayflow_pod_kinds_filter: 'ingestion_pod_kinds' must be a list."
        )
    out: list[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip()
        if not kind:
            continue
        out.append({
            "kind": kind,
            "source_kind": str(entry.get("source_kind") or "").strip() or None,
        })
    return out


def _pod_matches_filter(pod: Any, filters: list[Dict[str, Any]]) -> bool:
    pod_kind = str(getattr(pod, "kind", "") or "").strip()
    pod_meta = getattr(pod, "metadata", None) or {}
    pod_source_kind = ""
    if isinstance(pod_meta, dict):
        pod_source_kind = str(pod_meta.get("source_kind") or "").strip()
    for f in filters:
        if f["kind"] != pod_kind:
            continue
        required_source = f.get("source_kind")
        if required_source and required_source != pod_source_kind:
            continue
        return True
    return False


def _ingest_pods(
    existing_ids: set[str], now_utc: datetime,
) -> list[Message]:
    """Ingest new pods from pod_store as dayflow items.

    Filters by `ingestion_pod_kinds` allowlist in access.json. Pods of
    other kinds (manual uploads, email attachments, etc.) are not
    ingested — they remain available via pod_search but don't enter
    dayflow's working set unless explicitly allowed.
    """
    filters = _load_dayflow_pod_kinds_filter()
    if not filters:
        return []  # pod ingestion disabled

    from app.assistant.pod_store.pod_store import PodStore

    status = load_orchestrator_status()
    raw_watermark = status.get(POD_WATERMARK_KEY)
    since_utc: datetime | None = None
    if raw_watermark:
        try:
            since_utc = parse_iso_utc_strict(raw_watermark, label=POD_WATERMARK_KEY)
        except Exception as e:
            logger.warning("[pod_ingest] bad watermark %r, ignoring: %s", raw_watermark, e)
            since_utc = None
    if since_utc is None:
        # First run: cap at start-of-day so we don't replay history.
        since_utc = _get_day_start_utc(now_utc)

    store = PodStore()
    # Query without kind filter — we may have multiple kinds in the
    # allowlist; filter post-fetch by the (kind, source_kind) tuple.
    pods = store.query(since_utc=since_utc, limit=200)

    new: list[Message] = []
    max_seen_ts = since_utc
    for pod in pods:
        if not _pod_matches_filter(pod, filters):
            continue
        try:
            msg = _build_pod_message(pod=pod, now_utc=now_utc)
        except Exception as e:
            logger.error("[pod_ingest] failed to build message for pod %s: %s", pod.pod_id, e)
            continue
        item_id = str(getattr(msg, "id", "") or "").strip()
        if item_id and item_id not in existing_ids:
            new.append(msg)
        # Advance watermark even on dedup so we don't re-query the row.
        try:
            ts = pod.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > max_seen_ts:
                max_seen_ts = ts
        except Exception:
            pass

    if max_seen_ts > since_utc:
        status[POD_WATERMARK_KEY] = max_seen_ts.isoformat()
        persist_orchestrator_status(status)

    return new


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
    delegation_items, delegation_requests = _ingest_delegation_requests(existing_ids, now)
    pod_items = _ingest_pods(existing_ids, now)

    all_new = chat_items + email_items + delegation_items + pod_items

    if not all_new:
        logger.info("run_dayflow_ingestion: no new items to ingest.")
        return {
            "chat": 0, "email": 0, "delegation": 0, "pod": 0, "total": 0,
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
        "delegation": len(delegation_items),
        "pod": len(pod_items),
        "total": len(all_new),
    }
    logger.info(
        "run_dayflow_ingestion: ingested %d item(s). chat=%d email=%d delegation=%d pod=%d",
        summary["total"], summary["chat"], summary["email"],
        summary["delegation"], summary["pod"],
    )
    return summary
