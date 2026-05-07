from __future__ import annotations

"""
Sleep Computation Module

Pure computation - no file I/O.
Reads from DB tables, computes sleep data, returns dict.
The step handles output.
"""

from dataclasses import dataclass
from datetime import datetime, date, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import (
    ensure_aware_utc as _ensure_utc,
    get_local_timezone,
    local_to_utc,
    parse_iso_utc,
    utc_to_local,
)
from app.assistant.pipelines.dayflow.sleep.sleep_config import get_sleep_config, SleepConfig
from app.assistant.pipelines.dayflow.sleep.sleep_reconciliation import reconcile_sleep
from app.assistant.afk_manager import afk_db

logger = get_logger(__name__)


def _combine_local(d: date, t: time) -> datetime:
    tz = get_local_timezone()
    return datetime(d.year, d.month, d.day, t.hour, t.minute, 0, tzinfo=tz)


def _day_date_local(now_local: datetime, divider: time) -> date:
    """
    "Day date" is keyed by the divider.
    If local time is before divider, treat as previous day.
    """
    if now_local.timetz().replace(tzinfo=None) < divider:
        return now_local.date() - timedelta(days=1)
    return now_local.date()


def _sleep_window_start_local(day_date: date, cfg: SleepConfig) -> datetime:
    return _combine_local(day_date - timedelta(days=1), cfg.sleep_window_start)


def _sleep_window_end_local(day_date: date, cfg: SleepConfig) -> datetime:
    end_local = _combine_local(day_date, cfg.sleep_window_end)
    start_local = _sleep_window_start_local(day_date, cfg)
    if end_local <= start_local:
        end_local = end_local + timedelta(days=1)
    return end_local


def _divider_cutoff_local(day_date: date, cfg: SleepConfig) -> datetime:
    return _combine_local(day_date, cfg.sleep_awake_divider)


@dataclass(frozen=True)
class _Interval:
    start_utc: datetime
    end_utc: datetime


def _intersect_interval(a: _Interval, b_start: datetime, b_end: datetime) -> Optional[_Interval]:
    s = max(a.start_utc, b_start)
    e = min(a.end_utc, b_end)
    if e <= s:
        return None
    return _Interval(s, e)


def _filter_inferred_that_overlaps_user(
    inferred: List[Dict[str, Any]],
    user_sleep: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Rule you specified:
    If an inferred AFK-derived segment overlaps any user sleep segment,
    drop that inferred segment (for sleep consideration).
    """
    if not inferred or not user_sleep:
        return inferred

    def _parse(s: str) -> datetime:
        return parse_iso_utc(s)

    user_ranges: List[Tuple[datetime, datetime]] = []
    for u in user_sleep:
        try:
            st = _parse(u["start"])
            en = _parse(u["end"])
            if en > st:
                user_ranges.append((st, en))
        except Exception as e:
            logger.debug("Skipping user sleep record due to bad timestamp: %s", e, exc_info=True)
            continue

    if not user_ranges:
        return inferred

    kept: List[Dict[str, Any]] = []
    for inf in inferred:
        try:
            st = _parse(inf["start"])
            en = _parse(inf["end"])
        except Exception:
            kept.append(inf)
            continue

        overlaps = False
        for ust, uen in user_ranges:
            if st < uen and ust < en:
                overlaps = True
                break

        if not overlaps:
            kept.append(inf)

    return kept


def _calculate_sleep_score(
    total_sleep_minutes: float,
    segment_count: int,
    cfg: SleepConfig,
) -> Tuple[int, str]:
    if total_sleep_minutes <= 0:
        return 0, "terrible"

    scoring = cfg.get("sleep_quality_scoring", default={}) or {}

    ideal_min = float(scoring.get("ideal_min_minutes", 480))  # 8 hours
    ideal_max = float(scoring.get("ideal_max_minutes", 510))  # 8.5 hours

    duration_cfg = scoring.get("duration_scoring", {}) or {}
    points_per_hour_under = float(duration_cfg.get("points_per_hour_under", 27.5))
    points_per_hour_over = float(duration_cfg.get("points_per_hour_over", 8))
    min_duration_for_points = float(duration_cfg.get("min_duration_for_points", 60))

    frag_cfg = scoring.get("fragmentation_penalty", {}) or {}
    first_segment_penalty = float(frag_cfg.get("first_segment_penalty", 25))
    additional_segment_penalty = float(frag_cfg.get("additional_segment_penalty", 10))

    tier_thresholds = scoring.get("tier_thresholds", {}) or {}

    duration_score = 100.0

    if total_sleep_minutes < min_duration_for_points:
        duration_score = 5.0
    elif total_sleep_minutes < ideal_min:
        hours_under = (ideal_min - total_sleep_minutes) / 60.0
        duration_score = max(0.0, 100.0 - (hours_under * points_per_hour_under))
    elif total_sleep_minutes > ideal_max:
        hours_over = (total_sleep_minutes - ideal_max) / 60.0
        duration_score = max(0.0, 100.0 - (hours_over * points_per_hour_over))

    frag_penalty = 0.0
    if segment_count > 1:
        frag_penalty = first_segment_penalty
        if segment_count > 2:
            frag_penalty += (segment_count - 2) * additional_segment_penalty

    final_score = max(0, int(round(duration_score - frag_penalty)))

    tier = "terrible"
    tier_order = [
        ("excellent", tier_thresholds.get("excellent", 90)),
        ("great", tier_thresholds.get("great", 80)),
        ("very_good", tier_thresholds.get("very_good", 70)),
        ("good", tier_thresholds.get("good", 60)),
        ("pretty_good", tier_thresholds.get("pretty_good", 50)),
        ("ok", tier_thresholds.get("ok", 40)),
        ("mediocre", tier_thresholds.get("mediocre", 30)),
        ("poor", tier_thresholds.get("poor", 20)),
        ("bad", tier_thresholds.get("bad", 10)),
    ]

    for tier_name, threshold in tier_order:
        try:
            thr = float(threshold)
        except Exception:
            thr = 0.0
        if final_score >= thr:
            tier = tier_name
            break

    return final_score, tier


def _sleep_quality(total_sleep_minutes: float, cfg: SleepConfig) -> str:
    """Legacy helper: returns just the tier string."""
    _, tier = _calculate_sleep_score(total_sleep_minutes, 1, cfg)
    return tier


def _apply_sleep_segment_trim(
    start_utc: datetime,
    end_utc: datetime,
    cfg: SleepConfig,
) -> Optional[Tuple[datetime, datetime, float]]:
    """
    Apply realistic trim to a sleep segment derived from AFK.

    AFK doesn't perfectly reflect sleep:
    - user doesn't fall asleep instantly when going AFK
    - user doesn't race to computer immediately upon waking

    Returns (trimmed_start, trimmed_end, duration_minutes) or None if segment
    becomes invalid after trimming.
    """
    raw_duration_min = (end_utc - start_utc).total_seconds() / 60.0

    # Only apply trim to segments longer than threshold
    min_hours = float(getattr(cfg, "min_segment_hours_for_trim", 0) or 0)
    if raw_duration_min < (min_hours * 60.0):
        return start_utc, end_utc, raw_duration_min

    # Calculate trim amounts
    start_trim = float(getattr(cfg, "start_trim_minutes", 0) or 0)
    end_trim = float(getattr(cfg, "end_trim_minutes", 0) or 0)
    total_trim = start_trim + end_trim

    # Apply safety cap - never trim more than max_trim_percent
    max_trim_percent = float(getattr(cfg, "max_trim_percent", 0) or 0)
    max_trim = raw_duration_min * (max_trim_percent / 100.0)
    if total_trim > max_trim and total_trim > 0:
        scale = max_trim / total_trim
        start_trim *= scale
        end_trim *= scale
        total_trim = start_trim + end_trim

    trimmed_start = start_utc + timedelta(minutes=start_trim)
    trimmed_end = end_utc - timedelta(minutes=end_trim)

    if trimmed_end <= trimmed_start:
        logger.warning(
            "Sleep segment trim made segment invalid: raw=%.1fmin trim=%.1fmin",
            raw_duration_min,
            total_trim,
        )
        return None

    trimmed_duration = (trimmed_end - trimmed_start).total_seconds() / 60.0
    return trimmed_start, trimmed_end, trimmed_duration


def compute_sleep_data(*, now_utc: datetime, now_local: datetime) -> Dict[str, Any]:
    """
    Computes sleep for the current "day" as:
      tracking window = [sleep_window_start (last night), now]

    Rules:
    - AFK-derived sleep is inferred only inside the sleep window.
    - Sleep can extend past the divider until the first active segment after divider.
      If no legitimate wake by the sleep window end, cap at normal wake end.
    - Sleep outside the sleep window (naps) is only counted if the user reported it
      (SleepSegment source user_chat/manual).
    - Wake segments are subtracted (set subtraction), splitting sleep into fragments.
    - If an inferred AFK-derived sleep segment overlaps any user sleep segment,
      drop that inferred segment (do not partially keep it).
    """
    cfg = get_sleep_config()

    now_utc = _ensure_utc(now_utc)
    # Prefer caller-provided now_local to avoid repeated tz conversions.
    if now_local.tzinfo is None:
        now_local = utc_to_local(now_utc)

    day_date = _day_date_local(now_local, cfg.sleep_awake_divider)

    sleep_start_local = _sleep_window_start_local(day_date, cfg)
    sleep_end_local = _sleep_window_end_local(day_date, cfg)
    divider_local = _divider_cutoff_local(day_date, cfg)

    tracking_start_local = sleep_start_local
    tracking_end_local = now_local

    tracking_start_utc = local_to_utc(tracking_start_local)
    tracking_end_utc = _ensure_utc(local_to_utc(tracking_end_local))

    # Inference start is last night's window start (UTC).
    inference_start_utc = local_to_utc(sleep_start_local)
    sleep_end_utc = local_to_utc(sleep_end_local)

    # DB query padding to avoid boundary misses
    pad = timedelta(hours=12)
    query_start_utc = _ensure_utc(tracking_start_utc - pad)
    query_end_utc = _ensure_utc(tracking_end_utc + pad)

    active_segments = afk_db.get_active_segments_overlapping_range(
        start_utc=query_start_utc,
        end_utc=query_end_utc,
        include_provisional=True,
    )

    # User-reported sleep/wake segments (for naps and explicit corrections).
    # Policy: only treat sources in {"user_chat","manual"} as user overrides.
    # (Other sources may overlap inferred AFK segments and would double count.)
    from app.assistant.pipelines.dayflow.sleep import sleep_db

    def _clip(start: Optional[datetime], end: Optional[datetime]) -> Optional[Tuple[datetime, datetime]]:
        if not start:
            return None
        st = _ensure_utc(start)
        en = _ensure_utc(end) if end else tracking_end_utc
        st = max(st, query_start_utc)
        en = min(en, query_end_utc)
        if en <= st:
            return None
        return st, en

    user_sleep_segments: List[Dict[str, Any]] = []
    for row in sleep_db.get_sleep_segments_last_24_hours() or []:
        try:
            src = str(row.get("source") or "")
            if src not in ("user_chat", "manual"):
                continue
            st = parse_iso_utc(str(row.get("start")))
            en_val = row.get("end")
            en = parse_iso_utc(str(en_val)) if en_val else None
            clipped = _clip(st, en)
            if not clipped:
                continue
            stc, enc = clipped
            user_sleep_segments.append(
                {
                    "id": row.get("id"),
                    "start": stc.isoformat(),
                    "end": enc.isoformat(),
                    "duration_minutes": (enc - stc).total_seconds() / 60.0,
                    "source": src,
                    "raw_mention": row.get("raw_mention"),
                }
            )
        except Exception as e:
            logger.debug("Skipping sleep segment due to parse error: %s", e, exc_info=True)
            continue

    user_wake_segments: List[Dict[str, Any]] = []
    for row in sleep_db.get_wake_segments_last_24_hours() or []:
        try:
            st_val = row.get("start_time")
            if not st_val:
                continue
            st = parse_iso_utc(str(st_val))
            en_val = row.get("end_time")
            en = parse_iso_utc(str(en_val)) if en_val else None
            clipped = _clip(st, en)
            if not clipped:
                continue
            stc, enc = clipped
            user_wake_segments.append(
                {
                    "id": row.get("id"),
                    "start_time": stc.isoformat(),
                    "end_time": enc.isoformat(),
                    "duration_minutes": (enc - stc).total_seconds() / 60.0,
                    "source": row.get("source"),
                    "notes": row.get("notes"),
                }
            )
        except Exception as e:
            logger.debug("Skipping wake segment due to parse error: %s", e, exc_info=True)
            continue

    # Legitimate wake = first active segment start after ambiguous wake end.
    divider_utc = local_to_utc(divider_local)
    first_active_after_divider_utc: Optional[datetime] = None
    has_activity_in_window = False
    for seg in active_segments:
        try:
            st = _ensure_utc(seg.start_time)
            en = _ensure_utc(seg.end_time)
            if st <= sleep_end_utc and en >= inference_start_utc:
                has_activity_in_window = True
            if st >= divider_utc:
                if first_active_after_divider_utc is None or st < first_active_after_divider_utc:
                    first_active_after_divider_utc = st
        except Exception as e:
            logger.debug("Skipping active segment due to bad timestamp: %s", e, exc_info=True)
            continue

    # Sleep can extend past divider until the first active after divider.
    sleep_end_effective_utc = local_to_utc(sleep_end_local)
    if first_active_after_divider_utc is not None:
        sleep_end_effective_utc = min(sleep_end_effective_utc, first_active_after_divider_utc)

    # If there's no legitimate wake by the end of the sleep window, cap at
    # the configured normal wake end to avoid assuming sleep until 9 AM.
    normal_end_hhmm = str(cfg.get("normal_sleep", "end", default=cfg.sleep_window_end_hhmm()))
    try:
        eh, em = cfg.parse_hhmm(normal_end_hhmm)
    except Exception:
        eh, em = cfg.parse_hhmm(cfg.sleep_window_end_hhmm())

    normal_end_local = datetime(
        day_date.year,
        day_date.month,
        day_date.day,
        eh,
        em,
        0,
        tzinfo=get_local_timezone(),
    )
    normal_end_utc = local_to_utc(normal_end_local)

    if first_active_after_divider_utc is None and now_local >= sleep_end_local:
        if normal_end_local > sleep_start_local:
            sleep_end_effective_utc = min(sleep_end_effective_utc, normal_end_utc)

    inference_end_utc = min(tracking_end_utc, sleep_end_effective_utc)

    from app.assistant.afk_manager.afk_statistics import get_afk_intervals_overlapping_range

    # Derive AFK intervals from gaps between active segments (shared utility)
    afk_intervals = get_afk_intervals_overlapping_range(
        start_utc=tracking_start_utc,
        end_utc=tracking_end_utc,
        include_provisional=True,
    )

    inferred_sleep_segments: List[Dict[str, Any]] = []
    if inference_end_utc > inference_start_utc:
        for seg in afk_intervals:
            try:
                seg_start = parse_iso_utc(seg["start_utc"])
                seg_end = parse_iso_utc(seg["end_utc"])
            except Exception as e:
                logger.debug("Skipping AFK interval due to bad timestamp: %s", e, exc_info=True)
                continue

            # Policy: do NOT treat AFK intervals that are fully "after the divider" as sleep.
            #
            # Key nuance:
            # - If a sleep/AFK interval *started before* the divider (e.g., 11PM -> 8AM),
            #   we still consider its portion after 5AM to be sleep (continuous main sleep).
            # - But if the AFK interval *starts after* the divider (e.g., 6AM -> 7AM),
            #   we do not infer it as sleep (prevents confusing post-boundary naps/AFK).
            if seg_start >= divider_utc:
                continue

            clipped = _intersect_interval(
                _Interval(seg_start, seg_end),
                inference_start_utc,
                inference_end_utc,
            )
            if not clipped:
                continue

            trim_result = _apply_sleep_segment_trim(clipped.start_utc, clipped.end_utc, cfg)
            if trim_result is None:
                continue

            trimmed_start, trimmed_end, dur_min = trim_result
            if dur_min < float(cfg.min_sleep_afk_minutes):
                continue

            inferred_sleep_segments.append(
                {
                    "start": trimmed_start.isoformat(),
                    "end": trimmed_end.isoformat(),
                    "duration_minutes": dur_min,
                    "source": "inferred_sleep",
                }
            )

    # If there's no legitimate wake by the end of the sleep window,
    # fall back to a default sleep segment (normal_sleep start -> normal_sleep end).
    has_legit_wake = first_active_after_divider_utc is not None
    if (not has_legit_wake) and (not has_activity_in_window) and now_local >= sleep_end_local:
        normal_start_hhmm = str(cfg.get("normal_sleep", "start", default=cfg.sleep_window_start_hhmm()))
        try:
            sh, sm = cfg.parse_hhmm(normal_start_hhmm)
        except Exception:
            sh, sm = cfg.parse_hhmm(cfg.sleep_window_start_hhmm())

        default_start_local = datetime(
            day_date.year,
            day_date.month,
            day_date.day,
            sh,
            sm,
            0,
            tzinfo=get_local_timezone(),
        ) - timedelta(days=1)
        default_end_local = normal_end_local
        default_start_utc = local_to_utc(default_start_local)
        default_end_utc = normal_end_utc

        inferred_sleep_segments = [
            {
                "start": default_start_utc.isoformat(),
                "end": default_end_utc.isoformat(),
                "duration_minutes": (default_end_utc - default_start_utc).total_seconds() / 60.0,
                "source": "default_sleep",
            }
        ]

    inferred_sleep_segments = _filter_inferred_that_overlaps_user(inferred_sleep_segments, user_sleep_segments)

    reconciled = reconcile_sleep(
        inferred_sleep_segments=inferred_sleep_segments,
        user_sleep_segments=user_sleep_segments,
        user_wake_segments=user_wake_segments,
        merge_gap_minutes=2,
    )

    sleep_periods_out: List[Dict[str, Any]] = []
    for p in reconciled.get("sleep_periods", []) or []:
        try:
            st = parse_iso_utc(p["start"])
            en = parse_iso_utc(p["end"])
            st_local = utc_to_local(st)
            en_local = utc_to_local(en)

            # Store UTC timestamps as naive strings (matches existing resource style)
            sleep_periods_out.append(
                {
                    "start": st.replace(tzinfo=None).isoformat(),
                    "end": en.replace(tzinfo=None).isoformat(),
                    "duration_minutes": float(p.get("duration_minutes", 0.0) or 0.0),
                    "type": p.get("type", "sleep"),
                    "source": p.get("source", "unknown"),
                    "start_local": st_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                    "end_local": en_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                }
            )
        except Exception as e:
            logger.debug("Skipping sleep period from output due to parse/format error: %s", e, exc_info=True)
            continue

    total_sleep_minutes = float(reconciled.get("total_sleep_minutes", 0.0) or 0.0)
    primary_sleep_minutes = float(reconciled.get("primary_sleep_minutes", 0.0) or 0.0)
    total_wake_minutes = float(reconciled.get("total_wake_minutes", 0.0) or 0.0)
    time_in_bed_minutes = float(reconciled.get("time_in_bed_minutes", 0.0) or 0.0)
    fragmented = bool(reconciled.get("fragmented", False))

    # Bedtime and wake time are based on the combined sleep envelope after reconciliation.
    bedtime_previous_local: Optional[datetime] = None
    night_start_local: Optional[datetime] = None
    night_start_utc: Optional[datetime] = None
    wake_time_local: Optional[datetime] = None
    if sleep_periods_out:
        # parse_iso_utc returns aware UTC for both naive and offset-bearing
        # ISO strings — sleep_periods_out stores naive UTC (line 527-528),
        # so all values come back as aware UTC and min/max compare cleanly.
        starts = [parse_iso_utc(x["start"]) for x in sleep_periods_out]
        ends = [parse_iso_utc(x["end"]) for x in sleep_periods_out]
        if starts and ends:
            earliest_utc = min(starts)
            latest_utc = max(ends)
            bedtime_previous_local = utc_to_local(earliest_utc)
            wake_time_local = utc_to_local(latest_utc)
            night_start_utc = earliest_utc
            night_start_local = bedtime_previous_local

    wake_time_local_from_activity: Optional[datetime] = None
    if first_active_after_divider_utc is not None:
        wake_time_local_from_activity = utc_to_local(first_active_after_divider_utc)

    sleep_score, sleep_tier = _calculate_sleep_score(total_sleep_minutes, len(sleep_periods_out), cfg)

    tz = get_local_timezone()
    timezone_str = str(getattr(tz, "key", tz))

    data: Dict[str, Any] = {
        "timezone": timezone_str,
        "date": day_date.isoformat(),
        "total_sleep_minutes": round(total_sleep_minutes, 1),
        "last_night_sleep_minutes": round(total_sleep_minutes, 1),
        "main_sleep_minutes": round(primary_sleep_minutes, 1),
        "sleep_quality_score": int(sleep_score),
        "sleep_quality": sleep_tier,
        "fragmented": fragmented,
        "segment_count": int(len(sleep_periods_out)),
        "total_wake_minutes": round(total_wake_minutes, 1),
        "time_in_bed_minutes": round(time_in_bed_minutes, 1),
        "sleep_periods": sleep_periods_out,
        "wake_time": wake_time_local.strftime("%H:%M") if wake_time_local else None,
        "wake_time_today": wake_time_local.strftime("%Y-%m-%d %H:%M") if wake_time_local else None,
        "wake_time_today_local": wake_time_local.strftime("%Y-%m-%d %I:%M %p %Z") if wake_time_local else None,
        "bedtime_previous": bedtime_previous_local.strftime("%Y-%m-%d %H:%M") if bedtime_previous_local else None,
        "bedtime_previous_local": bedtime_previous_local.strftime("%Y-%m-%d %I:%M %p %Z") if bedtime_previous_local else None,
        "night_start_time": night_start_local.strftime("%Y-%m-%d %H:%M") if night_start_local else None,
        "night_start_time_local": night_start_local.strftime("%Y-%m-%d %I:%M %p %Z") if night_start_local else None,
        "night_start_time_utc": night_start_utc.isoformat() if night_start_utc else None,
        # Activity-based wake time (authoritative for day start)
        "wake_time_activity": wake_time_local_from_activity.strftime("%Y-%m-%d %H:%M")
        if wake_time_local_from_activity
        else None,
        "wake_time_activity_local": wake_time_local_from_activity.strftime("%Y-%m-%d %I:%M %p %Z")
        if wake_time_local_from_activity
        else None,
        # Reconciliation breakdowns
        "source_breakdown_minutes": reconciled.get("source_breakdown_minutes", {}),
    }

    return data

