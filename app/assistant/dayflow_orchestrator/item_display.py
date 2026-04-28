"""Shared utility for preparing dayflow items for LLM prompts.

Every prep node should call ``enrich_item_for_prompt()`` before putting
items on the blackboard.  This ensures all timestamps are local,
all fields are consistently named, and no UTC leaks into prompts.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

# (utc_key, local_key, strftime_format)
_TIMESTAMP_FIELDS = [
    ("created_at", "created_at_local", "%I:%M %p"),
    ("dispatched_at", "dispatched_at_local", "%I:%M %p"),
    ("reactivate_at", "reactivate_at_local", "%Y-%m-%d %I:%M %p"),
    ("reactivate_at_utc", "reactivate_at_local", "%Y-%m-%d %I:%M %p"),
    ("executed_at", "executed_at_local", "%I:%M %p"),
    ("closed_at", "closed_at_local", "%I:%M %p"),
    ("scheduled_start_utc", "scheduled_start_local", "%Y-%m-%d %I:%M %p"),
    ("scheduled_end_utc", "scheduled_end_local", "%Y-%m-%d %I:%M %p"),
]


def enrich_item_for_prompt(item: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare a dayflow item dict for LLM consumption.

    - Converts all UTC timestamp fields to local-time display strings.
    - Preserves original UTC values (never overwrites them).
    - Returns the item with enriched metadata (mutates in place).
    """
    meta = get_meta(item)
    if not isinstance(meta, dict):
        return item

    local_tz = get_local_timezone()

    for utc_key, local_key, fmt in _TIMESTAMP_FIELDS:
        if meta.get(local_key):
            continue  # Already converted.
        raw = meta.get(utc_key)
        if not raw:
            continue
        parsed = parse_iso_utc(str(raw))
        if parsed is not None:
            meta[local_key] = parsed.astimezone(local_tz).strftime(fmt)

    # Ensure the item wrapper has the enriched metadata.
    if isinstance(item, dict):
        item["metadata"] = meta
    return item


def enrich_items_for_prompt(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enrich a list of dayflow items for LLM prompts."""
    for item in items:
        enrich_item_for_prompt(item)
    return items
