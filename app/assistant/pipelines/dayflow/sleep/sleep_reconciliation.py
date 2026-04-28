from __future__ import annotations

"""
Sleep Reconciler

Goal:
Combine sleep intervals from multiple sources into a final set of sleep segments.

Rules:
1) User sleep segments (source in {"user_chat","manual"}) override inferred sleep only where they overlap.
   - Meaning: subtract user sleep intervals from inferred sleep intervals (keep disjoint inferred pieces).
2) Wake segments subtract from whatever sleep exists (user or inferred), only in the overlap region.
3) Return net sleep periods, wake interruptions, totals, fragmentation, and a basic source breakdown.

All times are UTC. Inputs can be dicts containing ISO timestamps.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

UTC = timezone.utc


@dataclass(frozen=True)
class Interval:
    start_utc: datetime
    end_utc: datetime
    source: str
    segment_id: Optional[int] = None
    notes: Optional[str] = None
    estimated: bool = False

    def duration_minutes(self) -> float:
        return max(0.0, (self.end_utc - self.start_utc).total_seconds() / 60.0)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return _ensure_utc(dt)
        except Exception:
            return None
    return None


def _as_sleep_interval(seg: Dict[str, Any]) -> Optional[Interval]:
    start = _parse_dt(seg.get("start"))
    end = _parse_dt(seg.get("end"))
    if not start or not end or end <= start:
        return None
    return Interval(
        start_utc=start,
        end_utc=end,
        source=str(seg.get("source") or "unknown"),
        segment_id=seg.get("id"),
    )


def _as_wake_interval(seg: Dict[str, Any]) -> Optional[Interval]:
    start = _parse_dt(seg.get("start_time"))
    end = _parse_dt(seg.get("end_time"))

    if not start:
        return None

    estimated = False
    if not end:
        dur = seg.get("duration_minutes", None)
        try:
            dur_min = float(dur)
        except Exception:
            dur_min = 0.0
        if dur_min <= 0:
            return None
        end = start + timedelta(minutes=dur_min)
        estimated = True

    if end <= start:
        return None

    return Interval(
        start_utc=start,
        end_utc=end,
        source=str(seg.get("source") or "user_chat"),
        segment_id=seg.get("id"),
        notes=seg.get("notes"),
        estimated=bool(seg.get("estimated", False) or estimated),
    )


def _merge(intervals: List[Interval], merge_gap_minutes: int = 0) -> List[Interval]:
    if not intervals:
        return []
    gap = timedelta(minutes=max(0, int(merge_gap_minutes)))
    items = sorted(intervals, key=lambda x: x.start_utc)

    out: List[Interval] = []
    cur = items[0]

    for nxt in items[1:]:
        if nxt.start_utc <= cur.end_utc + gap:
            cur = Interval(
                start_utc=min(cur.start_utc, nxt.start_utc),
                end_utc=max(cur.end_utc, nxt.end_utc),
                source=cur.source if cur.source == nxt.source else "mixed",
            )
        else:
            out.append(cur)
            cur = nxt

    out.append(cur)
    return out


def _subtract(base: List[Interval], cut: List[Interval]) -> List[Interval]:
    """
    Subtract all cut intervals from base intervals.
    Keeps disjoint remnants (this is the key behavior for user overriding inferred).
    """
    if not base:
        return []
    if not cut:
        return base

    cut_sorted = sorted(cut, key=lambda x: x.start_utc)
    result: List[Interval] = []

    for b in base:
        fragments = [b]
        for c in cut_sorted:
            new_frags: List[Interval] = []
            for f in fragments:
                if c.end_utc <= f.start_utc or c.start_utc >= f.end_utc:
                    new_frags.append(f)
                    continue

                # overlap: keep left piece
                if c.start_utc > f.start_utc:
                    new_frags.append(
                        Interval(
                            start_utc=f.start_utc,
                            end_utc=c.start_utc,
                            source=f.source,
                        )
                    )
                # overlap: keep right piece
                if c.end_utc < f.end_utc:
                    new_frags.append(
                        Interval(
                            start_utc=c.end_utc,
                            end_utc=f.end_utc,
                            source=f.source,
                        )
                    )
            fragments = new_frags
            if not fragments:
                break

        result.extend([x for x in fragments if x.end_utc > x.start_utc])

    return result


def _intersect_with_union(intervals: List[Interval], bounds: List[Interval]) -> List[Interval]:
    if not intervals or not bounds:
        return []
    union_bounds = _merge(bounds, merge_gap_minutes=0)

    out: List[Interval] = []
    for x in intervals:
        for b in union_bounds:
            s = max(x.start_utc, b.start_utc)
            e = min(x.end_utc, b.end_utc)
            if e > s:
                out.append(
                    Interval(
                        start_utc=s,
                        end_utc=e,
                        source=x.source,
                        notes=x.notes,
                        estimated=x.estimated,
                    )
                )
    return out


def reconcile_sleep(
    inferred_sleep_segments: List[Dict[str, Any]],
    user_sleep_segments: List[Dict[str, Any]],
    user_wake_segments: List[Dict[str, Any]],
    merge_gap_minutes: int = 2,
) -> Dict[str, Any]:
    """
    Returns:
      {
        "total_sleep_minutes": float,
        "total_wake_minutes": float,
        "sleep_periods": [ {start,end,duration_minutes,type,source} ... ],
        "wake_interruptions": [ {start,end,duration_minutes,notes,estimated,source} ... ],
        "fragmented": bool,
        "primary_sleep_minutes": float,
        "time_in_bed_minutes": float,
        "source_breakdown_minutes": { "user_sleep": x, "inferred_sleep": y }
      }
    """
    inferred_intervals = [x for x in (_as_sleep_interval(s) for s in inferred_sleep_segments) if x]
    user_intervals = [x for x in (_as_sleep_interval(s) for s in user_sleep_segments) if x]
    wake_intervals = [x for x in (_as_wake_interval(w) for w in user_wake_segments) if x]

    inferred_intervals = _merge(inferred_intervals, merge_gap_minutes=0)
    user_intervals = _merge(user_intervals, merge_gap_minutes=0)

    # Key rule: user overrides inferred only on overlap, keep disjoint inferred pieces.
    inferred_after_user = _subtract(inferred_intervals, user_intervals)

    combined_sleep = _merge(user_intervals + inferred_after_user, merge_gap_minutes=int(merge_gap_minutes))

    if not combined_sleep:
        return {
            "total_sleep_minutes": 0.0,
            "total_wake_minutes": 0.0,
            "sleep_periods": [],
            "wake_interruptions": [],
            "fragmented": False,
            "primary_sleep_minutes": 0.0,
            "time_in_bed_minutes": 0.0,
            "source_breakdown_minutes": {"user_sleep": 0.0, "inferred_sleep": 0.0},
        }

    # Wake interruptions only matter if they occur during the combined sleep envelope.
    wake_in_sleep = _intersect_with_union(wake_intervals, combined_sleep)
    wake_in_sleep = _merge(wake_in_sleep, merge_gap_minutes=int(merge_gap_minutes))

    net_sleep = _subtract(combined_sleep, wake_in_sleep)
    net_sleep = _merge(net_sleep, merge_gap_minutes=int(merge_gap_minutes))

    total_sleep = sum(x.duration_minutes() for x in net_sleep)
    total_wake = sum(x.duration_minutes() for x in wake_in_sleep)

    earliest = min(x.start_utc for x in combined_sleep)
    latest = max(x.end_utc for x in combined_sleep)
    time_in_bed = max(0.0, (latest - earliest).total_seconds() / 60.0)

    primary_sleep = max((x.duration_minutes() for x in net_sleep), default=0.0)
    fragmented = (len(net_sleep) > 1) or (len(wake_in_sleep) > 0)

    # Source breakdown: measure net minutes attributable to user vs inferred after user override.
    # We do this by subtracting wake from each source bucket separately.
    user_net = _merge(_subtract(user_intervals, wake_in_sleep), merge_gap_minutes=int(merge_gap_minutes))
    inferred_net = _merge(_subtract(inferred_after_user, wake_in_sleep), merge_gap_minutes=int(merge_gap_minutes))

    breakdown = {
        "user_sleep": round(sum(x.duration_minutes() for x in user_net), 1),
        "inferred_sleep": round(sum(x.duration_minutes() for x in inferred_net), 1),
    }

    def _iso(dt: datetime) -> str:
        return _ensure_utc(dt).isoformat()

    sleep_periods_out: List[Dict[str, Any]] = []
    for seg in net_sleep:
        seg_type = "main_sleep" if abs(seg.duration_minutes() - primary_sleep) < 1e-6 else "sleep"
        sleep_periods_out.append(
            {
                "start": _iso(seg.start_utc),
                "end": _iso(seg.end_utc),
                "duration_minutes": round(seg.duration_minutes(), 3),
                "type": seg_type,
                "source": seg.source,
            }
        )

    wake_out: List[Dict[str, Any]] = []
    for w in wake_in_sleep:
        wake_out.append(
            {
                "start": _iso(w.start_utc),
                "end": _iso(w.end_utc),
                "duration_minutes": round(w.duration_minutes(), 3),
                "notes": w.notes,
                "estimated": bool(w.estimated),
                "source": w.source,
            }
        )

    return {
        "total_sleep_minutes": round(total_sleep, 1),
        "total_wake_minutes": round(total_wake, 1),
        "sleep_periods": sleep_periods_out,
        "wake_interruptions": wake_out,
        "fragmented": bool(fragmented),
        "primary_sleep_minutes": round(primary_sleep, 1),
        "time_in_bed_minutes": round(time_in_bed, 1),
        "source_breakdown_minutes": breakdown,
    }

