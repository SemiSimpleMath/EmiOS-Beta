"""THE date-comparison module (fragility review #5).

Every KG consumer that compares node dates goes through here — the same
move as the importance module: one commissioned home for the logic, so
doctrine changes (and the eventual first-class partial-date migration)
touch ONE file instead of every call site.

Doctrine notes:
- SQLite hands back naive datetimes; columns are nominally aware UTC.
  ``as_aware_utc`` is the canonical normalizer — consumers never call
  ``.replace(tzinfo=...)`` themselves.
- Year-floor doctrine: a floored date ("2003" stored as 2003-01-01 +
  confidence estimated) compares AS ITS FLOOR. That is the standing
  semantic; precision-aware comparison arrives with the partial-date
  migration and will land here, invisible to consumers.
- Window semantics are half-open: start <= d < end, None = unbounded on
  that side (the era test used by the disambiguation temporal drain and
  succession splits).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# What callers/mutators may ASSIGN (kg_split_succession, date answers, ...).
VALID_DATE_CONFIDENCES = ("user_set", "actual", "estimated", "inferred")
# Everything legitimately in the wild — the assignable set plus system
# markers: 'auto_decay' (state decay TTL-closes write it), 'confirmed'
# (seed data), 'explicit' (legacy scheme; no current writer but the
# timeline still honors it as full precision). Lint validates against
# THIS; the first live lint run flagged 900+ auto_decay rows because the
# vocabularies were conflated.
KNOWN_DATE_CONFIDENCES = VALID_DATE_CONFIDENCES + ("auto_decay", "confirmed", "explicit")


def as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Canonical normalizer: naive datetimes are UTC by storage contract."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _now(now: Optional[datetime] = None) -> datetime:
    return as_aware_utc(now) if now is not None else datetime.now(timezone.utc)


def is_future(dt: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    """True when dt is strictly after now. None is never future."""
    a = as_aware_utc(dt)
    return a is not None and a > _now(now)


def is_past(dt: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    """True when dt is strictly before now. None is never past."""
    a = as_aware_utc(dt)
    return a is not None and a < _now(now)


def in_window(
    d: Optional[datetime],
    start: Optional[datetime],
    end: Optional[datetime],
) -> bool:
    """Half-open era test: start <= d < end; None bounds are unbounded.
    None d is in no window."""
    a = as_aware_utc(d)
    if a is None:
        return False
    s, e = as_aware_utc(start), as_aware_utc(end)
    return (s is None or s <= a) and (e is None or a < e)


def windows_overlap(
    start_a: Optional[datetime], end_a: Optional[datetime],
    start_b: Optional[datetime], end_b: Optional[datetime],
) -> bool:
    """Half-open interval overlap; None = unbounded on that side."""
    sa, ea = as_aware_utc(start_a), as_aware_utc(end_a)
    sb, eb = as_aware_utc(start_b), as_aware_utc(end_b)
    after_b_starts = eb is None or sa is None or sa < eb
    after_a_starts = ea is None or sb is None or sb < ea
    return after_b_starts and after_a_starts


def end_before_start(
    start: Optional[datetime], end: Optional[datetime],
) -> bool:
    """The impossible-era check: both set and end strictly before start."""
    s, e = as_aware_utc(start), as_aware_utc(end)
    return s is not None and e is not None and e < s
