"""
AFK Statistics Utility - Active-First Model

Computes presence metrics from ActiveSegment rows over a window [since_utc, now_utc]:
- total_active_time_minutes: sum of active session durations (bounded to window)
- total_afk_time_minutes: gaps between active sessions (bounded to window)
- active_work_session_minutes: current uninterrupted active session (if active now)
- current_afk_minutes: time since last active session ended (if AFK now)
- afk_count: number of AFK intervals in the window

Model:
- Active time is POSITIVE EVIDENCE: we only count time we KNOW user was active.
- No proven activity is treated as AFK. This is the conservative default —
  if we don't have evidence the user was at the keyboard, we assume they weren't.
- Current session state (is_currently_active, current_active_start_utc) comes
  from AFKMonitor and is passed in by the caller.

Usage:
- DayFlow's AFKStatisticsStage calls get_afk_statistics() with day boundary
- Passes is_currently_active and current_active_start_utc from AFKMonitor snapshot
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.afk_manager.afk_db import get_active_segments_overlapping_range

logger = get_logger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _minutes_between(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 60.0)


def get_afk_intervals_overlapping_range(
    start_utc: datetime,
    end_utc: datetime,
    include_provisional: bool = True,
) -> List[Dict[str, Any]]:
    """Return AFK intervals (gaps between active segments) within the range.

    If there are no active segments, the entire range is one AFK interval
    (no activity = AFK).

    Intervals are bounded by [start_utc, end_utc].
    """
    start_utc = _ensure_utc(start_utc)
    end_utc = _ensure_utc(end_utc)

    if end_utc <= start_utc:
        return []

    segments = get_active_segments_overlapping_range(
        start_utc=start_utc,
        end_utc=end_utc,
        include_provisional=include_provisional,
    )

    if not segments:
        return [
            {
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
                "duration_minutes": round(_minutes_between(start_utc, end_utc), 2),
            }
        ]

    out: List[Dict[str, Any]] = []
    sorted_segs = sorted(segments, key=lambda s: s.start_time)

    # Gap before first active segment.
    first_start = _ensure_utc(sorted_segs[0].start_time)
    if first_start > start_utc:
        out.append({
            "start_utc": start_utc.isoformat(),
            "end_utc": first_start.isoformat(),
            "duration_minutes": round(_minutes_between(start_utc, first_start), 2),
        })

    # Gaps between consecutive segments.
    for i in range(len(sorted_segs) - 1):
        try:
            prev_end = _ensure_utc(sorted_segs[i].end_time)
            next_start = _ensure_utc(sorted_segs[i + 1].start_time)
        except Exception as e:
            logger.debug("Skipping segment pair due to bad timestamp: %s", e, exc_info=True)
            continue

        if next_start > prev_end:
            gap_start = max(prev_end, start_utc)
            gap_end = min(next_start, end_utc)
            if gap_end > gap_start:
                out.append({
                    "start_utc": gap_start.isoformat(),
                    "end_utc": gap_end.isoformat(),
                    "duration_minutes": round(_minutes_between(gap_start, gap_end), 2),
                })

    # Gap after last active segment.
    last_end = _ensure_utc(sorted_segs[-1].end_time)
    if end_utc > last_end:
        out.append({
            "start_utc": last_end.isoformat(),
            "end_utc": end_utc.isoformat(),
            "duration_minutes": round(_minutes_between(last_end, end_utc), 2),
        })

    return out


def _create_empty_stats(now_utc: datetime, since_utc: datetime) -> Dict[str, Any]:
    """Return a zero-filled stats dict with the same schema as the normal path."""
    return {
        "total_active_time_minutes": 0.0,
        "total_afk_time_minutes": 0.0,

        "at_keyboard": False,
        "current_session_minutes": 0.0,

        "active_work_session_minutes": 0.0,
        "current_afk_minutes": 0.0,
        "is_currently_afk": True,

        "afk_count": 0,
        "active_segment_count": 0,

        "last_active_end_utc": None,

        "computed_at_utc": now_utc.isoformat(),
        "since_utc": since_utc.isoformat(),
        "source": "database",
    }


def get_afk_statistics(
        since_utc: Optional[datetime] = None,
        current_active_start_utc: Optional[datetime] = None,
        is_currently_active: bool = False,
) -> Dict[str, Any]:
    """Compute presence statistics from active segments.

    Args:
        since_utc: Start of observation window (default: 24h ago)
        current_active_start_utc: If user is currently active, when did session start?
        is_currently_active: Is user active right now?

    Returns:
        Statistics dictionary with consistent schema (same keys on success and error).
    """
    try:
        now_utc = _ensure_utc(datetime.now(timezone.utc))

        if since_utc is None:
            since_utc = now_utc - timedelta(hours=24)
        else:
            since_utc = _ensure_utc(since_utc)

        # Completed active segments in the window.
        segments = get_active_segments_overlapping_range(
            start_utc=since_utc,
            end_utc=now_utc,
            include_provisional=False,
        )

        # Sum active time from completed segments.
        total_active = 0.0
        last_active_end: Optional[datetime] = None

        for seg in segments:
            try:
                seg_start = _ensure_utc(seg.start_time)
                seg_end = _ensure_utc(seg.end_time)
            except Exception:
                continue

            if seg_end <= seg_start:
                continue

            overlap_start = max(seg_start, since_utc)
            overlap_end = min(seg_end, now_utc)

            if overlap_end > overlap_start:
                total_active += _minutes_between(overlap_start, overlap_end)

            if last_active_end is None or seg_end > last_active_end:
                last_active_end = seg_end

        # Current live session (not yet in DB).
        active_work_session_minutes = 0.0
        # Include current session in segment count.
        segment_count = len(segments)
        if is_currently_active and current_active_start_utc:
            current_start = _ensure_utc(current_active_start_utc)
            clipped_start = max(current_start, since_utc)
            if now_utc > clipped_start:
                active_work_session_minutes = _minutes_between(clipped_start, now_utc)
                total_active += active_work_session_minutes
            segment_count += 1

        # AFK intervals = gaps between active segments.
        total_afk = 0.0
        afk_count = 0

        afk_window_end = now_utc
        if is_currently_active and current_active_start_utc:
            current_start = _ensure_utc(current_active_start_utc)
            if current_start < afk_window_end:
                afk_window_end = current_start

        afk_intervals = get_afk_intervals_overlapping_range(
            start_utc=since_utc,
            end_utc=afk_window_end,
            include_provisional=False,
        )

        for gap in afk_intervals:
            try:
                gap_start = _ensure_utc(datetime.fromisoformat(gap["start_utc"].replace("Z", "+00:00")))
                gap_end = _ensure_utc(datetime.fromisoformat(gap["end_utc"].replace("Z", "+00:00")))
            except Exception as e:
                logger.debug("Skipping AFK interval due to bad timestamp: %s", e, exc_info=True)
                continue

            overlap_start = max(gap_start, since_utc)
            overlap_end = min(gap_end, afk_window_end)
            if overlap_end > overlap_start:
                total_afk += _minutes_between(overlap_start, overlap_end)
                afk_count += 1

        # Current AFK duration (if not active).
        current_afk_minutes = 0.0
        if not is_currently_active:
            last_end = last_active_end or since_utc
            if now_utc > last_end:
                current_afk_minutes = _minutes_between(max(last_end, since_utc), now_utc)

        return {
            "total_active_time_minutes": round(total_active, 1),
            "total_afk_time_minutes": round(total_afk, 1),

            "at_keyboard": is_currently_active,
            "current_session_minutes": round(active_work_session_minutes, 1),

            # Legacy keys kept for backward compatibility.
            "active_work_session_minutes": round(active_work_session_minutes, 1),
            "current_afk_minutes": round(current_afk_minutes, 1),
            "is_currently_afk": not is_currently_active,

            "afk_count": afk_count,
            "active_segment_count": segment_count,

            "last_active_end_utc": last_active_end.isoformat() if last_active_end else None,

            "computed_at_utc": now_utc.isoformat(),
            "since_utc": since_utc.isoformat(),
            "source": "database",
        }

    except Exception as e:
        logger.error("Error computing AFK statistics: %s", e)
        now_utc = _ensure_utc(datetime.now(timezone.utc))
        since_utc_safe = _ensure_utc(since_utc) if since_utc else (now_utc - timedelta(hours=24))
        return _create_empty_stats(now_utc, since_utc_safe)
