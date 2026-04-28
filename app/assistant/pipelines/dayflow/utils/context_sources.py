from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root, get_resources_dir
from app.assistant.utils.time_utils import parse_time_string, utc_to_local

logger = get_logger(__name__)


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        logger.debug("Could not parse datetime string: %r", value, exc_info=True)
        return None


def _load_calendar_events() -> List[Dict[str, Any]]:
    try:
        from app.assistant.event_repository.event_repository import EventRepositoryManager

        repo = EventRepositoryManager()
        events_json = repo.search_events(data_type="calendar")
        events_list = json.loads(events_json) if events_json else []

        parsed: List[Dict[str, Any]] = []
        for event in events_list:
            data = event.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    logger.debug("context_sources: skipping event with unparseable data field", exc_info=True)
                    continue

            start_iso = data.get("start", "")
            end_iso = data.get("end", "")
            start = _parse_utc(start_iso)
            end = _parse_utc(end_iso) if end_iso else None

            if not start:
                continue

            parsed.append(
                {
                    "id": data.get("id", ""),
                    "summary": data.get("summary", "Untitled"),
                    "start_utc": start,
                    "end_utc": end,
                    "attendees": data.get("attendees", []) or [],
                }
            )

        return parsed
    except Exception as e:
        logger.warning("Could not load calendar events: %s", e)
        return []


def get_calendar_events_structured_for_day(boundary_date_local: str) -> List[Dict[str, Any]]:
    """
    Return concrete structured calendar events overlapping the provided local day.
    This is intended to be consumed by downstream stateful systems (e.g. dayflow input layer).
    """
    raw_boundary = str(boundary_date_local or "").strip()
    if not raw_boundary:
        raise ValueError("boundary_date_local is required for structured calendar extraction.")
    try:
        boundary_date = datetime.strptime(raw_boundary, "%Y-%m-%d").date()
    except Exception as e:
        logger.error("Invalid boundary_date_local for structured calendar extraction: %s", raw_boundary)
        logger.debug("Structured calendar boundary_date_local parse exception details", exc_info=True)
        raise

    now_utc = datetime.now(timezone.utc)
    structured: List[Dict[str, Any]] = []
    for event in _load_calendar_events():
        start_utc = event.get("start_utc")
        end_utc = event.get("end_utc")
        if not isinstance(start_utc, datetime):
            continue
        if end_utc is not None and not isinstance(end_utc, datetime):
            raise ValueError(f"Calendar event has invalid end_utc type: {type(end_utc).__name__}")

        start_local_dt = utc_to_local(start_utc)
        end_local_dt = utc_to_local(end_utc) if end_utc else None
        start_local_date = start_local_dt.date()
        end_local_date = end_local_dt.date() if end_local_dt else start_local_date
        if boundary_date < start_local_date or boundary_date > end_local_date:
            continue

        if end_utc and now_utc > end_utc:
            status = "completed"
        elif now_utc >= start_utc and (end_utc is None or now_utc <= end_utc):
            status = "ongoing"
        else:
            status = "upcoming"

        source_id = str(event.get("id") or "").strip()
        if not source_id:
            seed = f"{event.get('summary','')}|{start_utc.isoformat()}|{end_utc.isoformat() if end_utc else ''}"
            source_id = f"calendar_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"

        structured.append(
            {
                "calendar_item_id": source_id,
                "title": str(event.get("summary", "Untitled") or "Untitled"),
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat() if end_utc else None,
                "start_local": start_local_dt.strftime("%I:%M %p"),
                "end_local": end_local_dt.strftime("%I:%M %p") if end_local_dt else "",
                "status": status,
                "source": "event_repository",
            }
        )

    structured.sort(key=lambda item: str(item.get("start_utc") or ""))
    return structured


def _load_sleep_output() -> Dict[str, Any]:
    try:
        resources_dir = get_resources_dir() / "dayflow_pipeline_outputs"
        sleep_path = resources_dir / "resource_sleep_output.json"
        if not sleep_path.exists():
            return {}
        with sleep_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Could not load sleep output: %s", e)
        return {}


def _format_local_time(dt: Optional[datetime], *, reference_date: Optional[date] = None) -> str:
    if not dt:
        return ""
    local_dt = utc_to_local(dt)
    time_str = local_dt.strftime("%I:%M %p")
    if reference_date and local_dt.date() != reference_date:
        time_str = local_dt.strftime("%b %d, %I:%M %p")
    return time_str


def _is_meeting_event(summary: str, attendees: List[Any]) -> bool:
    label = (summary or "").lower()
    return bool(attendees) or "meeting" in label or "call" in label


def get_calendar_events_upcoming_for_daily_context(hours: int = 12) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    today_local = utc_to_local(now).date()
    window_end = now + timedelta(hours=hours)
    result: List[Dict[str, Any]] = []

    for event in _load_calendar_events():
        start = event.get("start_utc")
        end = event.get("end_utc")
        if not start:
            continue
        if start > now and start < window_end:
            entry: Dict[str, Any] = {
                "summary": event.get("summary", "Untitled"),
                "start": _format_local_time(start, reference_date=today_local),
                "end": _format_local_time(end, reference_date=today_local),
            }
            raw_id = str(event.get("id") or "").strip()
            if raw_id:
                entry["calendar_item_id"] = raw_id
            result.append(entry)

    return result


def get_calendar_events_completed(hours: int = 4) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    result: List[Dict[str, Any]] = []

    for event in _load_calendar_events():
        end = event.get("end_utc")
        if not end:
            continue
        if cutoff <= end <= now:
            result.append(
                {
                    "event_name": event.get("summary", "Untitled"),
                    "end_time": _format_local_time(end),
                }
            )

    return result


def get_emails_for_orchestrator() -> List[Dict[str, Any]]:
    """
    Return today's emails from the event repo with importance >= 5, sorted newest first.

    "Today" is anchored to local midnight so the list stays stable throughout the day
    and does not drift with a rolling-hour window.
    """
    try:
        from email.utils import parsedate_to_datetime

        from app.assistant.event_repository.event_repository import EventRepositoryManager
        from app.assistant.utils.time_utils import utc_to_local

        now_utc = datetime.now(timezone.utc)
        now_local = utc_to_local(now_utc)
        today_midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        repo = EventRepositoryManager()
        raw_json = repo.search_events(data_type="email")
        all_events: list = json.loads(raw_json) if raw_json else []

        result: List[Dict[str, Any]] = []
        for event in all_events:
            data = event.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    logger.debug("context_sources: skipping event with unparseable data field", exc_info=True)
                    continue

            importance = data.get("importance")
            if importance is None or int(importance) < 5:
                continue

            date_str = (data.get("date_received") or "").strip()
            try:
                email_dt_local = utc_to_local(parsedate_to_datetime(date_str))
            except Exception:
                logger.debug("context_sources: could not parse email date_received: %r", date_str, exc_info=True)
                continue

            if email_dt_local < today_midnight_local:
                continue

            result.append(
                {
                    "sender": (data.get("sender") or "").strip(),
                    "subject": (data.get("subject") or "").strip(),
                    "summary": (data.get("summary") or "").strip(),
                    "importance": int(importance),
                    "time": email_dt_local.strftime("%I:%M %p"),
                    "action_items": data.get("action_items") or [],
                }
            )

        result.sort(key=lambda x: x["time"], reverse=True)
        return result
    except Exception as e:
        logger.warning("Could not load emails for orchestrator: %s", e)
        return []


def get_calendar_events_for_orchestrator(hours: int = 4) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=hours)
    result: List[Dict[str, Any]] = []

    for event in _load_calendar_events():
        start = event.get("start_utc")
        end = event.get("end_utc")
        if not start:
            continue
        if start > now and start < window_end:
            result.append(
                {
                    "event_name": event.get("summary", "Untitled"),
                    "start_time": _format_local_time(start),
                    "end_time": _format_local_time(end),
                }
            )

    return result


def get_calendar_events_for_health_inference(hours: int = 8) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=hours)
    result: List[Dict[str, Any]] = []

    for event in _load_calendar_events():
        start = event.get("start_utc")
        end = event.get("end_utc")
        if not start or not end:
            continue
        if end > now and start < window_end:
            result.append(
                {
                    "event_name": event.get("summary", "Untitled"),
                    "start_time": _format_local_time(start),
                    "end_time": _format_local_time(end),
                    "is_meeting": _is_meeting_event(event.get("summary", ""), event.get("attendees", [])),
                }
            )

    return result


def build_time_since() -> Dict[str, Any]:
    """
    Build time_since dict dynamically from tracked-activities config and output.

    Returns dict with:
    - minutes_since_{field_name}: float or None for each tracked activity
    - {field_name}_count_today: int for each tracked activity
    """
    try:
        tracked_config_path = (
            get_app_root() / "assistant" / "pipelines" / "dayflow" / "step_configs" / "config_tracked_activities.json"
        )

        result: Dict[str, Any] = {}

        # Seed all keys from config with defaults
        if tracked_config_path.exists():
            try:
                tracked_config = json.loads(tracked_config_path.read_text(encoding="utf-8")) or {}
                activities_cfg = tracked_config.get("activities", {}) if isinstance(tracked_config, dict) else {}
                if isinstance(activities_cfg, dict):
                    for activity_cfg in activities_cfg.values():
                        if not isinstance(activity_cfg, dict):
                            continue
                        field_name = activity_cfg.get("field_name")
                        if isinstance(field_name, str) and field_name:
                            result[f"minutes_since_{field_name}"] = None
                            result[f"{field_name}_count_today"] = 0
            except Exception as e:
                logger.warning("Could not read tracked activities config: %s", e)

        # Overlay actual values from output
        resources_dir = get_resources_dir() / "dayflow_pipeline_outputs"
        output_path = resources_dir / "resource_tracked_activities_output.json"
        output: Dict[str, Any] = {}
        if output_path.exists():
            output = json.loads(output_path.read_text(encoding="utf-8")) or {}

        activities = output.get("activities", {}) if isinstance(output, dict) else {}
        for field_name, data in activities.items():
            if not isinstance(data, dict):
                continue
            result[f"minutes_since_{field_name}"] = data.get("minutes_since")
            result[f"{field_name}_count_today"] = data.get("count_today", 0)

        return result
    except Exception as e:
        logger.warning("Could not build time_since: %s", e)
        return {}


def get_recent_afk_intervals(
    hours: int = 8,
    limit: int = 20,
    min_duration_minutes: float = 10.0,
) -> List[Dict[str, Any]]:
    """
    Get recent AFK intervals derived from gaps between active segments.

    "Recent" means AFK gaps overlapping the window from wake -> now.
    If wake time is unavailable, fallback to the last `hours` window.
    """
    try:
        from app.assistant.afk_manager.afk_statistics import get_afk_intervals_overlapping_range

        now_utc = datetime.now(timezone.utc)
        is_currently_afk = False
        current_afk_minutes: Optional[float] = None
        try:
            afk_path = get_resources_dir() / "dayflow_pipeline_outputs" / "resource_afk_statistics_output.json"
            if afk_path.exists():
                afk_data = json.loads(afk_path.read_text(encoding="utf-8")) or {}
                if isinstance(afk_data, dict):
                    is_currently_afk = bool(afk_data.get("is_afk", False))
                    cur = afk_data.get("current_afk_minutes")
                    if isinstance(cur, (int, float)):
                        current_afk_minutes = float(cur)
        except Exception as e:
            logger.warning(f"Could not load AFK stats output: {e}")

        sleep_output = _load_sleep_output()
        wake_utc = _parse_utc(sleep_output.get("day_start_time_utc"))
        if not wake_utc:
            wake_local = sleep_output.get("wake_time_today")
            if wake_local:
                try:
                    wake_utc = parse_time_string(wake_local)
                except Exception:
                    logger.debug("context_sources: could not parse wake_local time: %r", wake_local, exc_info=True)
                    wake_utc = None

        if not wake_utc or wake_utc >= now_utc:
            wake_utc = now_utc - timedelta(hours=hours)

        afk_intervals = get_afk_intervals_overlapping_range(
            start_utc=wake_utc,
            end_utc=now_utc,
            include_provisional=True,
        )

        if not afk_intervals:
            return []

        result: List[Dict[str, Any]] = []
        for interval in afk_intervals:
            start_utc = _parse_utc(interval.get("start_utc"))
            end_utc = _parse_utc(interval.get("end_utc"))
            if not start_utc or not end_utc or end_utc <= start_utc:
                continue

            # Trim to wake->now window for display and duration filtering.
            trimmed_start = max(start_utc, wake_utc)
            trimmed_end = min(end_utc, now_utc)
            if trimmed_end <= trimmed_start:
                continue

            duration = (trimmed_end - trimmed_start).total_seconds() / 60.0
            if duration < min_duration_minutes:
                continue

            is_ongoing = False
            if is_currently_afk:
                seconds_from_now = abs((now_utc - trimmed_end).total_seconds())
                if seconds_from_now <= 120:
                    is_ongoing = True
                    if current_afk_minutes is not None:
                        duration = current_afk_minutes

            start_local = utc_to_local(trimmed_start)
            end_local = "ongoing" if is_ongoing else utc_to_local(trimmed_end).strftime("%I:%M %p")
            result.append(
                {
                    "start_local": start_local.strftime("%I:%M %p"),
                    "end_local": end_local,
                    "duration_minutes": round(duration, 1),
                    "end_utc": trimmed_end.isoformat(),
                }
            )

        # Sort by most recent end time and limit.
        result.sort(key=lambda x: x["end_utc"], reverse=True)
        for item in result:
            item.pop("end_utc", None)
        return result[:limit]
    except Exception as e:
        logger.warning(f"Could not get AFK intervals: {e}")
        return []


def get_current_presence_status() -> Dict[str, Any]:
    """
    Get current user presence status for daily_context_generator.
    """
    try:
        from app.assistant.ServiceLocator.service_locator import DI

        now_utc = datetime.now(timezone.utc)
        result: Dict[str, Any] = {
            "is_afk": False,
            "status_label": "AT_KEYBOARD",
            "duration_minutes": 0.0,
            "start_time_local": "",
            "most_recent_afk": None,
        }

        monitor = getattr(DI, "afk_monitor", None)
        if monitor:
            snapshot = monitor.get_computer_activity()
            if isinstance(snapshot, dict):
                is_afk = bool(snapshot.get("is_afk", False))
                result["is_afk"] = is_afk
                result["status_label"] = "AWAY" if is_afk else "AT_KEYBOARD"

                if is_afk:
                    idle_mins = snapshot.get("idle_minutes") or snapshot.get("current_afk_minutes") or 0
                    result["duration_minutes"] = round(float(idle_mins), 1)
                    if idle_mins > 0:
                        start_utc = now_utc - timedelta(minutes=float(idle_mins))
                        result["start_time_local"] = utc_to_local(start_utc).strftime("%I:%M %p")
                else:
                    active_start_iso = snapshot.get("active_start_utc")
                    if active_start_iso:
                        active_start = _parse_utc(active_start_iso)
                        if active_start and active_start < now_utc:
                            active_mins = (now_utc - active_start).total_seconds() / 60.0
                            result["duration_minutes"] = round(active_mins, 1)
                            result["start_time_local"] = utc_to_local(active_start).strftime("%I:%M %p")

        # Most recent completed AFK interval (best-effort)
        try:
            from app.assistant.afk_manager.afk_statistics import get_afk_intervals_overlapping_range

            start_window = now_utc - timedelta(hours=8)
            afk_intervals = get_afk_intervals_overlapping_range(
                start_utc=start_window,
                end_utc=now_utc,
                include_provisional=False,
            )
            if afk_intervals:
                sorted_intervals = sorted(
                    afk_intervals, key=lambda x: x.get("end_utc", ""), reverse=True
                )
                for interval in sorted_intervals:
                    start_utc = _parse_utc(interval.get("start_utc"))
                    end_utc = _parse_utc(interval.get("end_utc"))
                    if start_utc and end_utc and end_utc > start_utc:
                        duration = (end_utc - start_utc).total_seconds() / 60.0
                        if duration >= 5:
                            result["most_recent_afk"] = {
                                "start_local": utc_to_local(start_utc).strftime("%I:%M %p"),
                                "end_local": utc_to_local(end_utc).strftime("%I:%M %p"),
                                "duration_minutes": round(duration, 0),
                            }
                            break
        except Exception as e:
            logger.debug("Could not get recent AFK intervals: %s", e, exc_info=True)

        return result
    except Exception as e:
        logger.warning("Could not get presence status: %s", e)
        return {
            "is_afk": False,
            "status_label": "UNKNOWN",
            "duration_minutes": 0.0,
            "start_time_local": "",
            "most_recent_afk": None,
        }


def get_desktop_activity_recent() -> str:
    """
    Returns a short text snippet describing what the user was doing on their desktop recently.

    Source files (preferred first):
    - resources/resource_desktop_activity_recent.md (single latest entry)
    - resources/resource_activity_log.md (append-only; we tail it)
    """
    resources_dir = get_resources_dir()
    candidates = [
        resources_dir / "dayflow_pipeline_outputs" / "resource_desktop_activity_recent.md",
        resources_dir / "instructions" / "resource_activity_log.md",
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            return text
        except Exception as e:
            logger.debug("Could not read desktop activity log from %s: %s", path, e)
            continue
    return ""


def get_significant_activity_segments(
    since_utc: datetime,
    noise_threshold_minutes: float = 5.0,
    merge_gap_minutes: float = 10.0,
) -> List[str]:
    """
    Get significant ACTIVE and AFK segments since a given time.

    Computes AFK from gaps between active segments to ensure consistency (no overlaps).

    Args:
        since_utc: Start time (UTC)
        noise_threshold_minutes: Filter out segments shorter than this (default 5 min)
        merge_gap_minutes: Merge active segments if gap between them is < this (default 10 min)

    Returns:
        List of formatted strings describing significant segments
    """
    from app.assistant.afk_manager.afk_db import get_active_segments_overlapping_range

    now_utc = datetime.now(timezone.utc)
    since_utc = _parse_utc(since_utc.isoformat()) if isinstance(since_utc, datetime) else _parse_utc(since_utc)

    if not since_utc or since_utc >= now_utc:
        return []

    try:
        # Get active segments and sort by start time
        active_segments = get_active_segments_overlapping_range(
            start_utc=since_utc,
            end_utc=now_utc,
            include_provisional=True,
        )

        # Parse and clip ALL active segments to window (no noise filter yet).
        all_active: List[Dict[str, Any]] = []
        for seg in active_segments:
            start = _parse_utc(seg.start_time.isoformat()) if seg.start_time else None
            end = _parse_utc(seg.end_time.isoformat()) if seg.end_time else None
            if start and end and end > start:
                start = max(start, since_utc)
                end = min(end, now_utc)
                if end > start:
                    duration = (end - start).total_seconds() / 60.0
                    all_active.append({"start": start, "end": end, "duration": duration})

        all_active.sort(key=lambda x: x["start"])

        # Build a full timeline of alternating Active/AFK segments.
        raw_timeline: List[Dict[str, Any]] = []
        cursor = since_utc
        for seg in all_active:
            if seg["start"] > cursor:
                raw_timeline.append({"start": cursor, "end": seg["start"], "type": "AFK"})
            raw_timeline.append({"start": seg["start"], "end": seg["end"], "type": "Active"})
            cursor = seg["end"]
        if cursor < now_utc:
            raw_timeline.append({"start": cursor, "end": now_utc, "type": "AFK"})

        # Absorb short blips into their neighbors.
        # If a segment is shorter than noise_threshold and is sandwiched
        # between segments of the same type, merge all three.
        # AFK → short Active → AFK  =  AFK (the active blip was a glance)
        # Active → short AFK → Active  =  Active (the AFK was a bathroom break)
        _BLIP_THRESHOLD = noise_threshold_minutes
        changed = True
        while changed:
            changed = False
            i = 1
            while i < len(raw_timeline) - 1:
                seg = raw_timeline[i]
                dur = (seg["end"] - seg["start"]).total_seconds() / 60.0
                prev_type = raw_timeline[i - 1]["type"]
                next_type = raw_timeline[i + 1]["type"]

                if dur < _BLIP_THRESHOLD and prev_type == next_type:
                    # Absorb: merge prev + current + next into one segment of prev's type.
                    raw_timeline[i - 1]["end"] = raw_timeline[i + 1]["end"]
                    del raw_timeline[i:i + 2]
                    changed = True
                else:
                    i += 1

        # Collapse runs of short alternating segments into "Intermittent".
        # If 3+ consecutive segments are all shorter than merge_gap_minutes
        # and alternate types (Active/AFK/Active/AFK...), collapse the run
        # into a single "Intermittent" block.
        _INTERMITTENT_MIN_SEGMENTS = 3
        i = 0
        collapsed: List[Dict[str, Any]] = []
        while i < len(raw_timeline):
            # Look ahead for a run of short alternating segments.
            run_end = i
            while run_end < len(raw_timeline):
                dur = (raw_timeline[run_end]["end"] - raw_timeline[run_end]["start"]).total_seconds() / 60.0
                if dur >= merge_gap_minutes:
                    break
                run_end += 1

            run_length = run_end - i
            if run_length >= _INTERMITTENT_MIN_SEGMENTS:
                # Collapse this run into one "Intermittent" segment.
                collapsed.append({
                    "start": raw_timeline[i]["start"],
                    "end": raw_timeline[run_end - 1]["end"],
                    "type": "Intermittent",
                })
                i = run_end
            else:
                collapsed.append(raw_timeline[i])
                i += 1

        # Filter to significant segments (>= noise_threshold).
        merged: List[Dict[str, Any]] = []
        for seg in collapsed:
            dur = (seg["end"] - seg["start"]).total_seconds() / 60.0
            if dur >= noise_threshold_minutes:
                merged.append({
                    "start_utc": seg["start"],
                    "end_utc": seg["end"],
                    "type": seg["type"],
                })

        # Add current state if not already covered (for < 20 min current state)
        if merged:
            last_end = merged[-1]["end_utc"]
            seconds_since_last = (now_utc - last_end).total_seconds()
        else:
            seconds_since_last = (now_utc - since_utc).total_seconds()

        if seconds_since_last > 120:  # Gap of more than 2 min
            # Get current state from AFKMonitor
            try:
                from app.assistant.ServiceLocator.service_locator import DI

                monitor = getattr(DI, "afk_monitor", None)
                if monitor:
                    snapshot = monitor.get_computer_activity()
                    if isinstance(snapshot, dict):
                        is_afk = bool(snapshot.get("is_afk", False))
                        current_type = "AFK" if is_afk else "Active"

                        if is_afk:
                            mins = float(snapshot.get("idle_minutes") or snapshot.get("current_afk_minutes") or 0)
                        else:
                            mins = float(snapshot.get("active_work_session_minutes") or 0)

                        if mins > 0:
                            current_start = now_utc - timedelta(minutes=mins)
                            merged.append(
                                {
                                    "start_utc": current_start,
                                    "end_utc": now_utc,
                                    "type": current_type,
                                    "is_current": True,
                                }
                            )
            except Exception as e:
                logger.debug(
                    "Could not append current AFKMonitor state to merged segments: %s",
                    e,
                    exc_info=True,
                )

        if not merged:
            return []

        # Format as strings
        result: List[str] = []
        for i, seg in enumerate(merged):
            start_local = utc_to_local(seg["start_utc"]).strftime("%I:%M %p").lstrip("0")

            is_last = i == len(merged) - 1
            is_current = seg.get("is_current", False)
            seconds_from_now = abs((now_utc - seg["end_utc"]).total_seconds())
            is_ongoing = is_last and (is_current or seconds_from_now < 120)

            if is_ongoing:
                duration_mins = int((now_utc - seg["start_utc"]).total_seconds() / 60)
                result.append(f"{start_local} {seg['type']} ({duration_mins} min, ongoing)")
            else:
                end_local = utc_to_local(seg["end_utc"]).strftime("%I:%M %p").lstrip("0")
                result.append(f"{start_local} - {end_local} {seg['type']}")

        return result

    except Exception as e:
        logger.warning(f"Could not get significant activity segments: {e}")
        return []


def parse_quiet_hours(quiet_hours_local: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in quiet_hours_local or []:
        if not isinstance(item, str) or "-" not in item:
            continue
        a, b = item.split("-", 1)
        try:
            start = parse_time_string(a.strip())
            end = parse_time_string(b.strip())
        except Exception:
            continue
        out.append({"start": start, "end": end})
    return out


# -----------------------------------------------------------------------------
# Ticket context helpers
# -----------------------------------------------------------------------------


def _format_ticket_for_context(ticket) -> Dict[str, Any]:
    """
    Format a Ticket object into a dict for agent context.

    Includes: title, message, suggestion_type, responded_at_local,
    snooze_until_local, user_comment
    """
    responded_at = getattr(ticket, "responded_at", None)
    snooze_until = getattr(ticket, "snooze_until", None)

    responded_local = utc_to_local(responded_at) if responded_at else None
    snooze_until_local = utc_to_local(snooze_until) if snooze_until else None

    trigger_context = getattr(ticket, "trigger_context", None) or {}
    if not isinstance(trigger_context, dict):
        trigger_context = {}
    acted_on_item_ids = trigger_context.get("acted_on_item_ids")
    if not isinstance(acted_on_item_ids, list):
        acted_on_item_ids = []

    return {
        "title": ticket.title or "",
        "message": ticket.message or "",
        "suggestion_type": ticket.suggestion_type or "",
        "responded_at_local": responded_local.strftime("%I:%M %p") if responded_local else "",
        "snooze_until_local": snooze_until_local.strftime("%I:%M %p") if snooze_until_local else "",
        "user_comment": ticket.user_text or "",
        "acted_on_item_ids": acted_on_item_ids,
        "execution_result": getattr(ticket, "execution_result", "") or "",
    }


def get_responded_tickets_categorized(since_utc: datetime, ticket_type: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get tickets user responded to since given time, categorized by response type.

    Pass ticket_type to scope results (e.g. "cron_reminder", "dayflow_decision").
    Without it, all responded tickets are returned.

    Filtering is the caller's responsibility — this function categorizes
    whatever the DB query returns.

    Categories:
    - accepted: done/willdo/accept/accepted
    - acknowledged: acknowledge
    - declined: skip/no/decline/declined/dismiss/dismissed
    - snoozed: later/snooze/snoozed
    """
    try:
        from app.assistant.ticket_manager import get_ticket_manager, TicketState

        tickets = get_ticket_manager().get_tickets(
            since_responded_utc=since_utc,
            states=[TicketState.ACCEPTED, TicketState.DISMISSED, TicketState.SNOOZED],
            ticket_type=ticket_type,
            order_by="responded_at",
            limit=50,
        )

        result: Dict[str, List[Dict[str, Any]]] = {
            "accepted": [],
            "acknowledged": [],
            "declined": [],
            "snoozed": [],
        }

        for t in tickets:
            formatted = _format_ticket_for_context(t)
            action = (t.user_action or "").lower()

            if action in ("done", "willdo", "accept", "accepted"):
                result["accepted"].append(formatted)
            elif action == "acknowledge":
                result["acknowledged"].append(formatted)
            elif action in ("skip", "no", "decline", "declined", "dismiss", "dismissed"):
                result["declined"].append(formatted)
            elif action in ("later", "snooze", "snoozed"):
                result["snoozed"].append(formatted)
            else:
                # Unknown action: log and skip rather than crashing the entire context block.
                logger.warning(
                    "get_responded_tickets_categorized: unknown user_action=%r for ticket %r — skipping",
                    action, t.title,
                )

        return result
    except ValueError:
        raise
    except Exception as e:
        logger.warning("Could not get responded tickets: %s", e)
        logger.debug("Could not get responded tickets exception details", exc_info=True)
        return {"accepted": [], "acknowledged": [], "declined": [], "snoozed": []}

