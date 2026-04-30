"""
Dayflow blackboard context builder (minimal).

Per-agent pre-LLM nodes now call ``get_dayflow_items()`` directly for
context, and wait_interrupt_promoter_node reads active dispatches via
``dispatch_sweeper.list_active_dispatches()`` at the point of use. This
module emits ``day_of_week`` and provides ``enrich_items_with_local_times``
for callers that need to decorate item metadata with local-time strings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone

logger = get_logger(__name__)


def enrich_items_with_local_times(items: List[Dict[str, Any]], now_utc: datetime) -> None:
    """Add local time strings and relative durations to each item's metadata.

    Mutates in place.
    """
    local_tz = get_local_timezone()
    for item in items:
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
        if meta is None:
            continue
        for utc_key, local_key, rel_key in (
                ("scheduled_start_utc", "scheduled_start_local", "starts_in"),
                ("scheduled_end_utc", "scheduled_end_local", "ends_in"),
                ("reactivate_at_utc", "reactivate_at_local", "wakes_in"),
                ("created_at", "created_at_local", "created_ago"),
                ("dispatched_at", "dispatched_at_local", "dispatched_ago"),
                ("executed_at", "executed_at_local", "executed_ago"),
        ):
            raw = meta.get(utc_key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                local_dt = parsed.astimezone(local_tz)
                meta[local_key] = local_dt.strftime("%Y-%m-%d %H:%M %Z")
                delta_secs = int((parsed - now_utc).total_seconds())
                if delta_secs < 0:
                    h, m = divmod(abs(delta_secs) // 60, 60)
                    meta[rel_key] = f"{h}h {m}m ago" if h else f"{m}m ago"
                else:
                    h, m = divmod(delta_secs // 60, 60)
                    meta[rel_key] = f"in {h}h {m}m" if h else f"in {m}m"
            except Exception as e:
                logger.error(
                    "enrich_items_with_local_times: failed parsing %s=%r for item_id=%s: %s",
                    utc_key, raw, meta.get("item_id", "?"), e,
                )
                logger.debug("enrich_items_with_local_times parse exception details", exc_info=True)
                raise


def build_dayflow_blackboard_extras() -> Dict[str, Any]:
    """Build the minimal context dict for the dayflow orchestrator blackboard.

    Most agent context is now prepared by per-agent pre-LLM nodes and by
    dispatch_sweeper.list_active_dispatches(). This function emits only
    day_of_week.
    """
    from zoneinfo import ZoneInfo

    now_utc = datetime.now(timezone.utc)
    local_tz = ZoneInfo("America/Los_Angeles")
    now_local = now_utc.astimezone(local_tz)

    return {
        "day_of_week": now_local.strftime("%A"),
    }
