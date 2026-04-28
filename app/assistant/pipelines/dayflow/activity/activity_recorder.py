from __future__ import annotations

"""
Activity Recorder (pipeline-native)

Responsibilities:
- Maintain "tracked activities" state in DayFlow pipeline state (ctx.state)
- Build an output payload suitable for dayflow_orchestrator input

Non-responsibilities:
- Writing files directly (steps must call ctx.write_resource)
- Updating DI caches directly (ctx.write_resource handles that)
"""

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root
from app.assistant.utils.time_utils import parse_iso_utc, utc_to_local

logger = get_logger(__name__)

_SCHEMA_VERSION = 2
_DEFAULT_COLD_START_GRACE_MINUTES = 15
_SEED_MAX_AGE_HOURS = 12

TRACKED_CONFIG_FILE = (
    get_app_root() / "assistant" / "pipelines" / "dayflow" / "step_configs" / "config_tracked_activities.json"
)


@dataclass(frozen=True)
class TrackedActivityDef:
    field_name: str
    display_name: str
    threshold_minutes: Optional[int]
    init_on_cold_start: bool
    reset_on_afk: bool
    guidance: Optional[str]
    show_daily_count: bool


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _local_date_str(dt_utc: datetime) -> str:
    return utc_to_local(dt_utc).strftime("%Y-%m-%d")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _existing_output_is_fresh(output: Dict[str, Any], now_utc: datetime) -> bool:
    try:
        computed_at = parse_iso_utc(output.get("computed_at_utc"))
        if not computed_at:
            return False
        age_hours = (now_utc - computed_at).total_seconds() / 3600.0
        return age_hours <= float(_SEED_MAX_AGE_HOURS)
    except Exception:
        return False


class ActivityRecorder:
    """
    Tracks activity timestamps and per-day counts, and builds an orchestrator-ready payload.
    """

    def __init__(self, state: Dict[str, Any], *, seed_output: Optional[Dict[str, Any]] = None):
        self.state = state
        self._lock = threading.Lock()
        self._defs = self._load_tracked_defs()

        with self._lock:
            # Cold-start: seed from the most recent output (if fresh) so restarts
            # don't wipe "last_occurrence_utc" / counts.
            self._ensure_state_initialized(now_utc=datetime.now(timezone.utc), seed_output=seed_output)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_tracked_defs(self) -> Dict[str, TrackedActivityDef]:
        defs: Dict[str, TrackedActivityDef] = {}
        if not TRACKED_CONFIG_FILE.exists():
            logger.error("Tracked activities config NOT FOUND at %s - activity tracking disabled", TRACKED_CONFIG_FILE)
            return defs

        try:
            raw = json.loads(TRACKED_CONFIG_FILE.read_text(encoding="utf-8")) or {}
            activities = raw.get("activities", {}) if isinstance(raw, dict) else {}
            if not isinstance(activities, dict):
                return defs

            for _, a in activities.items():
                if not isinstance(a, dict):
                    continue

                field_name = (a.get("field_name") or "").strip()
                if not field_name:
                    continue

                display_name = (a.get("display_name") or field_name).strip()
                init_on_cold_start = bool(a.get("init_on_cold_start", False))
                reset_on_afk = bool(a.get("reset_on_afk", False))

                guidance = a.get("guidance")
                guidance = str(guidance).strip() if isinstance(guidance, str) and guidance.strip() else None
                show_daily_count = bool(a.get("show_daily_count", False))

                threshold_minutes = None
                threshold = a.get("threshold")
                if isinstance(threshold, dict):
                    m = threshold.get("minutes")
                    if isinstance(m, (int, float)) and m > 0:
                        threshold_minutes = int(m)

                defs[field_name] = TrackedActivityDef(
                    field_name=field_name,
                    display_name=display_name,
                    threshold_minutes=threshold_minutes,
                    init_on_cold_start=init_on_cold_start,
                    reset_on_afk=reset_on_afk,
                    guidance=guidance,
                    show_daily_count=show_daily_count,
                )

            return defs
        except Exception as e:
            logger.error("Failed to load tracked activities config: %s", e)
            return {}

    def reload_config(self) -> None:
        with self._lock:
            self._defs = self._load_tracked_defs()
            self._ensure_state_initialized(now_utc=datetime.now(timezone.utc), seed_output=None)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _ensure_state_initialized(self, *, now_utc: datetime, seed_output: Optional[Dict[str, Any]]) -> None:
        if "tracked_activities_state" not in self.state or not isinstance(self.state.get("tracked_activities_state"), dict):
            self.state["tracked_activities_state"] = {}

        st = self.state["tracked_activities_state"]
        if "activities" not in st or not isinstance(st.get("activities"), dict):
            st["activities"] = {}

        st["schema_version"] = _SCHEMA_VERSION
        activities_state: Dict[str, Any] = st["activities"]

        today_local = _local_date_str(now_utc)

        output_activities: Dict[str, Any] = {}
        if isinstance(seed_output, dict) and _existing_output_is_fresh(seed_output, now_utc):
            out = seed_output.get("activities", {})
            output_activities = out if isinstance(out, dict) else {}

        for field_name, d in self._defs.items():
            if field_name not in activities_state or not isinstance(activities_state.get(field_name), dict):
                seed = output_activities.get(field_name, {}) if isinstance(output_activities, dict) else {}

                last_occurrence_utc = seed.get("last_occurrence_utc") or seed.get("last_utc")
                count_today = seed.get("count_today", 0)
                count_date_local = seed.get("count_date_local")

                activities_state[field_name] = {
                    "last_occurrence_utc": last_occurrence_utc if isinstance(last_occurrence_utc, str) else None,
                    "count_today": _safe_int(count_today, 0),
                    "count_date_local": count_date_local if isinstance(count_date_local, str) and count_date_local else today_local,
                    "last_reset_utc": now_utc.isoformat(),
                    "last_reset_reason": "init",
                    "suppress_overdue_until_utc": None,
                }

            astate = activities_state[field_name]

            if "last_occurrence_utc" not in astate:
                astate["last_occurrence_utc"] = None
            if "count_today" not in astate:
                astate["count_today"] = 0
            if "count_date_local" not in astate:
                astate["count_date_local"] = today_local
            if "last_reset_utc" not in astate:
                astate["last_reset_utc"] = now_utc.isoformat()
            if "last_reset_reason" not in astate:
                astate["last_reset_reason"] = "init"
            if "suppress_overdue_until_utc" not in astate:
                astate["suppress_overdue_until_utc"] = None

            # Cold start: add a short suppression window if configured and last is missing.
            if d.init_on_cold_start and not astate.get("last_occurrence_utc"):
                suppress_until = _ensure_utc(now_utc + timedelta(minutes=_DEFAULT_COLD_START_GRACE_MINUTES))
                astate["suppress_overdue_until_utc"] = suppress_until.isoformat()

    def get_state(self) -> Dict[str, Any]:
        try:
            return json.loads(json.dumps(self.state.get("tracked_activities_state", {})))
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def record_occurrence(self, field_name: str, *, timestamp_utc: datetime) -> None:
        with self._lock:
            now_utc = _ensure_utc(timestamp_utc)
            self._ensure_state_initialized(now_utc=now_utc, seed_output=None)
            st = self.state["tracked_activities_state"]
            activities_state = st.get("activities", {}) if isinstance(st, dict) else {}
            astate = activities_state.get(field_name)
            if not isinstance(astate, dict):
                return

            today_local = _local_date_str(now_utc)
            if astate.get("count_date_local") != today_local:
                astate["count_today"] = 0
                astate["count_date_local"] = today_local

            astate["last_occurrence_utc"] = now_utc.isoformat()
            astate["count_today"] = _safe_int(astate.get("count_today", 0), 0) + 1
            astate["suppress_overdue_until_utc"] = None
            st["last_updated_utc"] = now_utc.isoformat()

    def reset_for_new_day(self, boundary_date_local: str, *, boundary_utc: Optional[datetime] = None) -> None:
        """
        Daily boundary reset.
        - count_today -> 0
        - count_date_local -> boundary_date_local
        - last_occurrence_utc:
            - for threshold-based activities, set to boundary time so they become due after threshold
            - for non-threshold activities, keep None
        """
        now_utc = datetime.now(timezone.utc)
        boundary_dt = _ensure_utc(boundary_utc) if boundary_utc else _ensure_utc(now_utc)

        with self._lock:
            self._ensure_state_initialized(now_utc=_ensure_utc(now_utc), seed_output=None)
            st = self.state["tracked_activities_state"]
            activities_state: Dict[str, Any] = st.get("activities", {}) if isinstance(st.get("activities"), dict) else {}

            for field_name, d in self._defs.items():
                last_occ = boundary_dt.isoformat() if d.threshold_minutes is not None else None
                activities_state[field_name] = {
                    "last_occurrence_utc": last_occ,
                    "count_today": 0,
                    "count_date_local": boundary_date_local,
                    "last_reset_utc": boundary_dt.isoformat(),
                    "last_reset_reason": "daily_boundary",
                    "suppress_overdue_until_utc": None,
                }

            st["activities"] = activities_state
            st["last_updated_utc"] = _ensure_utc(now_utc).isoformat()

    def reset_on_afk_return(self, *, field_names: Optional[list[str]] = None, timestamp_utc: Optional[datetime] = None) -> None:
        now_utc = _ensure_utc(timestamp_utc or datetime.now(timezone.utc))

        if field_names is None:
            field_names = [f for f, d in self._defs.items() if d.reset_on_afk]

        with self._lock:
            self._ensure_state_initialized(now_utc=now_utc, seed_output=None)
            st = self.state["tracked_activities_state"]
            activities_state: Dict[str, Any] = st.get("activities", {}) if isinstance(st.get("activities"), dict) else {}

            for field_name in field_names:
                if field_name not in self._defs:
                    continue
                cur = activities_state.get(field_name) if isinstance(activities_state.get(field_name), dict) else {}
                cur.update(
                    {
                        "last_occurrence_utc": now_utc.isoformat(),
                        "last_reset_utc": now_utc.isoformat(),
                        "last_reset_reason": "afk_return",
                        "suppress_overdue_until_utc": None,
                    }
                )
                activities_state[field_name] = cur

            st["activities"] = activities_state
            st["last_updated_utc"] = now_utc.isoformat()

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def build_output_payload(self, *, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
        now = _ensure_utc(now_utc or datetime.now(timezone.utc))
        with self._lock:
            self._ensure_state_initialized(now_utc=now, seed_output=None)
            st = self.state.get("tracked_activities_state", {}) if isinstance(self.state.get("tracked_activities_state"), dict) else {}
            activities_state = st.get("activities", {}) if isinstance(st, dict) else {}

            out: Dict[str, Any] = {
                "schema_version": 2,
                "computed_at_utc": now.isoformat(),
                "computed_at_local": utc_to_local(now).strftime("%Y-%m-%d %I:%M %p"),
                "activities": {},
            }

            for field_name, d in self._defs.items():
                s = activities_state.get(field_name, {}) if isinstance(activities_state.get(field_name), dict) else {}

                last_iso = s.get("last_occurrence_utc")
                last_dt = parse_iso_utc(last_iso) if isinstance(last_iso, str) else None

                reset_iso = s.get("last_reset_utc")
                reset_dt = parse_iso_utc(reset_iso) if isinstance(reset_iso, str) else None

                suppress_until_iso = s.get("suppress_overdue_until_utc")
                suppress_until_dt = parse_iso_utc(suppress_until_iso) if isinstance(suppress_until_iso, str) else None

                minutes_since = None
                if last_dt:
                    minutes_since = max(0.0, (now - last_dt).total_seconds() / 60.0)

                minutes_since_reset = None
                if reset_dt:
                    minutes_since_reset = max(0.0, (now - reset_dt).total_seconds() / 60.0)

                threshold = d.threshold_minutes

                is_overdue = None
                next_due = None

                suppression_active = bool(suppress_until_dt and now < suppress_until_dt)

                if threshold is not None:
                    if suppression_active:
                        is_overdue = False
                        next_due = max(0.0, float(threshold))
                    elif minutes_since is None:
                        is_overdue = False
                        next_due = 0.0
                    else:
                        is_overdue = minutes_since >= float(threshold)
                        next_due = max(0.0, float(threshold) - minutes_since)

                out["activities"][field_name] = {
                    "display_name": d.display_name,
                    "threshold_minutes": threshold,
                    "minutes_since": round(minutes_since, 1) if isinstance(minutes_since, (int, float)) else None,
                    "is_overdue": bool(is_overdue) if is_overdue is not None else None,
                    "next_due_in_minutes": round(next_due, 1) if isinstance(next_due, (int, float)) else None,
                    "count_today": _safe_int(s.get("count_today", 0), 0),
                    "count_date_local": s.get("count_date_local"),
                    "last_occurrence_utc": last_iso if isinstance(last_iso, str) else None,
                    "last_reset_utc": reset_iso if isinstance(reset_iso, str) else None,
                    "minutes_since_reset": round(minutes_since_reset, 1) if isinstance(minutes_since_reset, (int, float)) else None,
                    "last_reset_reason": s.get("last_reset_reason"),
                    "suppress_overdue_until_utc": suppress_until_iso if isinstance(suppress_until_iso, str) else None,
                    "guidance": d.guidance,
                    "show_daily_count": d.show_daily_count,
                }

            return out

