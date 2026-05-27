from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc_strict

logger = get_logger(__name__)


def build_expected_calendar_markdown(expected_schedule: List[Dict[str, Any]]) -> str:
    """Render expected schedule into concise markdown for LLM consumption."""
    lines: List[str] = ["## Expected Calendar"]
    if not expected_schedule:
        lines.append("- [05:00 AM - 12:00 AM] User's schedule is wide open.")
        return "\n".join(lines) + "\n"

    for item in expected_schedule:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip() or "Untitled"
        start_local = str(item.get("start_local") or "").strip()
        end_local = str(item.get("end_local") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        time_label = start_local
        if start_local and end_local:
            time_label = f"{start_local} - {end_local}"
        elif end_local and not start_local:
            time_label = f"? - {end_local}"
        if not time_label:
            lines.append(f"- {title}")
            continue
        status_suffix = f" ({status})" if status else ""
        lines.append(f"- [{time_label}] {title}{status_suffix}")

    if len(lines) == 1:
        lines.append("- [05:00 AM - 12:00 AM] User's schedule is wide open.")
    return "\n".join(lines) + "\n"


def normalize_expected_schedule_to_utc(
    expected_schedule: List[Dict[str, Any]],
    *,
    boundary_date_local: str,
) -> List[Dict[str, Any]]:
    """Normalize schedule entries to include UTC timestamps where possible."""
    if not isinstance(expected_schedule, list):
        raise ValueError("expected_schedule must be a list.")

    boundary_date = datetime.strptime(boundary_date_local, "%Y-%m-%d").date()
    local_tz = get_local_timezone()
    normalized: List[Dict[str, Any]] = []

    for item in expected_schedule:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip() or "Untitled"
        start_local = str(item.get("start_local") or "").strip()
        end_local = str(item.get("end_local") or "").strip()
        status = str(item.get("status") or "upcoming").strip().lower() or "upcoming"
        # Preserve free-form source (no lower-casing) so agent-emitted tags like
        # "concern:Memorial Day delays Irvine trash pickup by a day" round-trip
        # intact. Default to "google_calendar" if missing.
        source = str(item.get("source") or "google_calendar").strip() or "google_calendar"
        updated_at_local = str(item.get("updated_at_local") or "").strip()
        calendar_item_id = str(item.get("calendar_item_id") or "").strip()

        start_utc = _resolve_start_utc(item, boundary_date=boundary_date, local_tz=local_tz)
        end_utc = _resolve_end_utc(
            item,
            boundary_date=boundary_date,
            local_tz=local_tz,
            start_utc=start_utc,
        )

        normalized.append(
            {
                "title": title,
                "start_local": start_local,
                "end_local": end_local,
                "status": status,
                "source": source,
                "updated_at_local": updated_at_local,
                "calendar_item_id": calendar_item_id,
                "start_utc": start_utc.isoformat() if isinstance(start_utc, datetime) else "",
                "end_utc": end_utc.isoformat() if isinstance(end_utc, datetime) else "",
            }
        )

    return normalized


def _resolve_start_utc(item: Dict[str, Any], *, boundary_date, local_tz) -> Optional[datetime]:
    start_utc_raw = str(item.get("start_utc") or "").strip()
    if start_utc_raw:
        return parse_iso_utc_strict(start_utc_raw, label="expected_schedule.start_utc")
    start_local = str(item.get("start_local") or "").strip()
    if not start_local:
        return None
    return _parse_local_clock(boundary_date, start_local, local_tz)


def _resolve_end_utc(
    item: Dict[str, Any],
    *,
    boundary_date,
    local_tz,
    start_utc: Optional[datetime],
) -> Optional[datetime]:
    end_utc_raw = str(item.get("end_utc") or "").strip()
    if end_utc_raw:
        return parse_iso_utc_strict(end_utc_raw, label="expected_schedule.end_utc")
    end_local = str(item.get("end_local") or "").strip()
    if not end_local:
        return None
    end_dt = _parse_local_clock(boundary_date, end_local, local_tz)
    if start_utc is not None and end_dt <= start_utc:
        # Event crossed midnight (e.g., 10:00 PM -> 12:00 AM).
        end_dt = end_dt + timedelta(days=1)
    return end_dt


def _parse_local_clock(boundary_date, value: str, local_tz) -> datetime:
    parsed = datetime.strptime(value, "%I:%M %p")
    local_dt = datetime(
        boundary_date.year,
        boundary_date.month,
        boundary_date.day,
        parsed.hour,
        parsed.minute,
        tzinfo=local_tz,
    )
    return local_dt.astimezone(timezone.utc)
