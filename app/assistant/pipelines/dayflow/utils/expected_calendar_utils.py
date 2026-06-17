from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc, parse_iso_utc_strict

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


# --- Hour-by-hour scaffold for the dayflow routine overlay --------------------
# The routine writer used to be told to "walk the window hour by hour" and do
# the clock math itself in prose — which mis-fired (e.g. judging 06:00 "already
# past" at 05:57). The schedule already carries structured UTC times, so we
# build the hour grid deterministically here and hand the writer a ready
# scaffold to FILL. Time-from-prose placement (which belief lands in which hour)
# stays with the LLM — that is the one thing we do NOT want to regex out of text.

def _fmt_clock(dt: datetime) -> str:
    """'6:00 AM' — Windows-safe (avoids the non-portable %-I)."""
    return dt.strftime("%I:%M %p").lstrip("0")


def end_of_local_day_utc(now_utc: datetime, *, local_tz=None) -> datetime:
    """UTC instant of the next local midnight (end of the local day holding now)."""
    if local_tz is None:
        local_tz = get_local_timezone()
    now_local = now_utc.astimezone(local_tz)
    next_midnight = (now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_midnight.astimezone(timezone.utc)


def build_hour_grid(
    items: List[Dict[str, Any]],
    now_utc: datetime,
    end_utc: Optional[datetime] = None,
    *,
    lookback_hours: int = 1,
    local_tz=None,
) -> List[Dict[str, Any]]:
    """Deterministic hour-by-hour skeleton spanning ~``lookback_hours`` before
    now through ``end_utc`` (default: end of the local day). One slot per LOCAL
    hour, INCLUDING hours with no scheduled item ('open' slots); each item is
    anchored to the local hour it starts in; the slot holding ``now`` is flagged
    ``is_now``. The writer FILLS slots — it never builds the grid or does clock
    math. The ~1h look-back lets a just-missed automation still be caught up.
    """
    if local_tz is None:
        local_tz = get_local_timezone()
    if end_utc is None:
        end_utc = end_of_local_day_utc(now_utc, local_tz=local_tz)

    now_local = now_utc.astimezone(local_tz)
    start_local = (now_utc - timedelta(hours=lookback_hours)).astimezone(local_tz).replace(
        minute=0, second=0, microsecond=0
    )
    end_local = end_utc.astimezone(local_tz)
    end_hour = end_local.replace(minute=0, second=0, microsecond=0)
    if end_local > end_hour:
        end_hour += timedelta(hours=1)

    by_hour: Dict[datetime, List[Dict[str, Any]]] = defaultdict(list)
    for it in items:
        if not isinstance(it, dict):
            continue
        start = parse_iso_utc(it.get("start_utc"))
        if start is None:
            continue
        hour_key = start.astimezone(local_tz).replace(minute=0, second=0, microsecond=0)
        by_hour[hour_key].append(it)

    slots: List[Dict[str, Any]] = []
    cur = start_local
    while cur < end_hour:
        is_now = cur <= now_local < cur + timedelta(hours=1)
        items_in = sorted(by_hour.get(cur, []), key=lambda it: str(it.get("start_utc") or ""))
        slots.append({
            "label": f"Now ({_fmt_clock(now_local)})" if is_now else _fmt_clock(cur),
            "hour_local_iso": cur.isoformat(),
            "is_now": is_now,
            "items": items_in,
        })
        cur += timedelta(hours=1)
    return slots


def render_hour_grid(slots: List[Dict[str, Any]]) -> str:
    """Render build_hour_grid() output as the scaffold the writer fills."""
    lines = [
        "Hour-by-hour scaffold. The 'Now (...)' slot is the CURRENT time; every "
        "slot from there on is UPCOMING (not yet done). Write an entry for EVERY "
        "slot below; (open) hours still get their timed beliefs or a brief ramp note:",
    ]
    for slot in slots:
        items = slot.get("items") or []
        if items:
            rendered = []
            for it in items:
                title = str(it.get("title") or "Untitled").strip()
                sl = str(it.get("start_local") or "").strip()
                el = str(it.get("end_local") or "").strip()
                status = str(it.get("status") or "").strip().lower()
                span = f" ({sl}–{el})" if sl and el else (f" ({sl})" if sl else "")
                suffix = f" [{status}]" if status and status != "upcoming" else ""
                rendered.append(f"{title}{span}{suffix}")
            body = "; ".join(rendered)
        else:
            body = "(open)"
        arrow = " →" if slot.get("is_now") else ""
        lines.append(f"  {slot['label']}{arrow}   {body}")
    return "\n".join(lines)
