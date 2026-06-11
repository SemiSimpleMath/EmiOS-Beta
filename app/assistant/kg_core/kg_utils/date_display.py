"""Precision-aware display formatting for KG node dates.

Year-only and month-only knowledge is stored FLOORED (YYYY-01-01 /
YYYY-MM-01) with confidence ``estimated``/``inferred`` purely so dates
sort and compare in SQL — the exact day is NOT known; the floored day
exists only to give a sense of chronology (user directive 2026-06-10,
born from the "wedding dated 2003-01-01" incident). The real precision
lives in the ``*_prose`` fields and the confidence marker.

Rendering must never fabricate that precision back: a floored estimated
date displays as "2003" or "2003-09", never as "2003-01-01".

``sort_key_ts`` lives here too so projections sort on the raw datetime
while displaying the honest form (also retires the duplicated
``_fmt_date`` / ``_sort_key_ts`` pairs found by the 2026-06-10 audit).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Confidences whose dates may be precision-floored placeholders. ``actual`` /
# ``user_set`` / ``explicit`` dates are real calendar days and render fully.
_FLOOR_CONFIDENCES = {"estimated", "inferred"}


def format_node_date(dt: Optional[datetime], confidence: Optional[str] = None) -> Optional[str]:
    """Format a node date at its HONEST precision.

    estimated/inferred + Jan 1   -> "YYYY"     (year-only knowledge)
    estimated/inferred + day 1   -> "YYYY-MM"  (month-only knowledge)
    everything else              -> "YYYY-MM-DD"
    """
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        if (confidence or "").strip().lower() in _FLOOR_CONFIDENCES:
            if dt.month == 1 and dt.day == 1:
                return str(dt.year)
            if dt.day == 1:
                return f"{dt.year}-{dt.month:02d}"
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(dt)


def sort_key_ts(dt: Optional[datetime]) -> float:
    """Mixed tz-aware/tz-naive datetimes can't be compared directly.
    Normalize to a POSIX timestamp for sort purposes only.
    ``None`` becomes 0 (so callers can order empty dates last/first as they wish)."""
    if dt is None:
        return 0.0
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0
