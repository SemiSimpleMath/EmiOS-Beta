# stages/sleep_stage.py
"""
Sleep Step

Flow:
1. Load step config
2. Compute sleep data from DB segments
3. Determine whether the day has started (user is up), and the day_start_time_utc
4. Output: Write resource_sleep_output.json and resource_sleep_segments_output.json

Notes:
- Computation stage (no LLM), safe to run every cycle.
- Determines day_start_time_utc which downstream stages depend on.
- Outputs both LOCAL display strings and UTC ISO strings for downstream math.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, local_to_utc, parse_iso_utc, utc_to_local
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult

logger = get_logger(__name__)


class SleepStep(BaseStep):
    """
    Pipeline stage for sleep tracking.

    Also determines whether the user is "up" for the day:
    - Day starts at the first active segment start after the awake divider (local time).
    - If no activity after divider, and current time is past sleep window end,
      day starts at the configured typical wake time (fallback).
    """

    step_id: str = "sleep"

    def _output_filename(self) -> str:
        return "resource_sleep_output.json"

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        return True, "ready"

    def _pre_sleep_snapshot_path(self, ctx: StepContext, boundary_date_local: str) -> Path:
        year = boundary_date_local[:4]
        month = boundary_date_local[5:7]
        # Archive per-day artifacts outside `resources/` so resources remains for injection.
        repo_root = Path(ctx.resources_dir).parent
        return (
            repo_root
            / "day_context"
            / year
            / month
            / boundary_date_local
            / "sleep_pre_sleep_snapshot.json"
        )

    def _maybe_write_pre_sleep_snapshot(self, ctx: StepContext, sleep_output: Dict[str, Any]) -> bool:
        """
        Harden a sleep snapshot shortly BEFORE the configured sleep window start.

        Rationale:
        - Sleep computations have "night windows" and can change after bedtime starts.
        - Capturing right before bedtime produces a stable daily record that includes naps.
        """
        step_cfg = ctx.step_config or {}
        snap_cfg = step_cfg.get("pre_sleep_snapshot", {}) if isinstance(step_cfg, dict) else {}
        if not isinstance(snap_cfg, dict) or not snap_cfg.get("enabled", True):
            return False

        minutes_before = int(snap_cfg.get("window_minutes_before", 15))
        minutes_after = int(snap_cfg.get("window_minutes_after", 1))

        boundary_date = self._boundary_date_local(ctx)
        out_path = self._pre_sleep_snapshot_path(ctx, boundary_date)
        if out_path.exists():
            return False

        try:
            from app.assistant.pipelines.dayflow.sleep.sleep_config import get_sleep_config

            cfg = get_sleep_config()
            start_t = getattr(cfg, "sleep_window_start", None)
            if start_t is None:
                return False
            tz = get_local_timezone()
            y, m, d = (int(x) for x in boundary_date.split("-"))
            target_local = datetime(
                y,
                m,
                d,
                int(getattr(start_t, "hour", 22)),
                int(getattr(start_t, "minute", 30)),
                0,
                tzinfo=tz,
            )
        except Exception:
            return False

        lo = target_local - timedelta(minutes=max(0, minutes_before))
        hi = target_local + timedelta(minutes=max(0, minutes_after))
        if not (lo <= ctx.now_local <= hi):
            return False

        payload = {
            "schema_version": 1,
            "boundary_date_local": boundary_date,
            "target_sleep_window_start_local": target_local.strftime("%Y-%m-%d %H:%M"),
            "snapshot_time_local": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
            "snapshot_time_utc": ctx.now_utc.isoformat(),
            "sleep_output": sleep_output,
            "notes": "Pre-sleep hardened snapshot captured near sleep_window.start to preserve naps/daily sleep stats before bedtime roll-in.",
        }
        write_json_atomic(out_path, payload)

        # Write a small "latest pointer" into resources/ for injection/debugging.
        try:
            pointer_path = Path(ctx.resources_dir) / "resource_sleep_pre_sleep_snapshot_latest.json"
            write_json_atomic(
                pointer_path,
                {
                    "schema_version": 1,
                    "boundary_date_local": boundary_date,
                    "snapshot_time_utc": ctx.now_utc.isoformat(),
                    "snapshot_time_local": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
                    "archive_path": str(out_path),
                },
            )
        except Exception:
            pass

        return True

    # -------------------------------------------------------------------------
    # Day-start logic
    # -------------------------------------------------------------------------

    def _divider_boundary_local(self, ctx: StepContext, divider: Any) -> datetime:
        """
        Build the most recent divider boundary in LOCAL time.
        divider is expected to behave like a datetime.time (has hour/minute).
        """
        now_local = ctx.now_local
        tz = get_local_timezone()

        boundary_local = datetime(
            now_local.year,
            now_local.month,
            now_local.day,
            int(getattr(divider, "hour", 0)),
            int(getattr(divider, "minute", 0)),
            0,
            tzinfo=tz,
        )

        # If we are before today's divider, boundary is yesterday's divider.
        divider_hm_now = (now_local.hour, now_local.minute)
        divider_hm = (boundary_local.hour, boundary_local.minute)
        if divider_hm_now < divider_hm:
            boundary_local = boundary_local - timedelta(days=1)

        return boundary_local

    def _get_typical_wake_local(self, cfg: Any, day_date, tz) -> datetime:
        """
        Best-effort: pick a typical wake time (local) for fallback start.
        Prefers cfg.normal_sleep_end if it exists; otherwise uses cfg.sleep_window_end.
        """
        # Try "normal_sleep_end" style first
        hhmm: Optional[str] = None
        normal_sleep = getattr(cfg, "normal_sleep", None)
        if normal_sleep is not None:
            try:
                hhmm = getattr(normal_sleep, "end", None) or (
                    normal_sleep.get("end") if hasattr(normal_sleep, "get") else None
                )
            except Exception:
                hhmm = None

        if not hhmm:
            try:
                hhmm = cfg.sleep_window_end_hhmm()
            except Exception:
                hhmm = None

        if hhmm:
            try:
                th, tm = cfg.parse_hhmm(str(hhmm))
                return datetime(day_date.year, day_date.month, day_date.day, th, tm, 0, tzinfo=tz)
            except Exception:
                logger.debug("SleepStep: could not parse typical wake hhmm=%s", hhmm, exc_info=True)

        swe = getattr(cfg, "sleep_window_end", None)
        if swe is not None:
            return datetime(
                day_date.year,
                day_date.month,
                day_date.day,
                int(getattr(swe, "hour", 7)),
                int(getattr(swe, "minute", 0)),
                0,
                tzinfo=tz,
            )

        return datetime(day_date.year, day_date.month, day_date.day, 7, 0, 0, tzinfo=tz)

    def _check_user_is_up(self, ctx: StepContext) -> Tuple[bool, Optional[datetime]]:
        """
        Returns (user_is_up, day_start_time_utc).
        """
        from app.assistant.afk_manager.afk_db import get_recent_active_segments
        from app.assistant.pipelines.dayflow.sleep.sleep_config import get_sleep_config

        tz = get_local_timezone()
        cfg = get_sleep_config()

        divider = getattr(cfg, "sleep_awake_divider", None)
        if divider is None:

            class _Tmp:
                hour = 5
                minute = 30

            divider = _Tmp()

        boundary_local = self._divider_boundary_local(ctx, divider)
        boundary_utc = local_to_utc(boundary_local)

        segments = get_recent_active_segments(hours=24) or []
        first_active_after_boundary: Optional[datetime] = None

        for seg in segments:
            ts_str = seg.get("start_time")
            if not ts_str:
                continue
            ts = parse_iso_utc(ts_str)
            if not ts:
                continue
            if ts >= boundary_utc and (first_active_after_boundary is None or ts < first_active_after_boundary):
                first_active_after_boundary = ts

        if first_active_after_boundary:
            return True, first_active_after_boundary

        swe = getattr(cfg, "sleep_window_end", None)
        if swe is None:

            class _Tmp2:
                hour = 10
                minute = 0

            swe = _Tmp2()

        day_date = boundary_local.date()
        sleep_window_end_local = datetime(
            day_date.year,
            day_date.month,
            day_date.day,
            int(getattr(swe, "hour", 10)),
            int(getattr(swe, "minute", 0)),
            0,
            tzinfo=tz,
        )

        if ctx.now_local >= sleep_window_end_local:
            typical_wake_local = self._get_typical_wake_local(cfg, day_date, tz)
            return True, local_to_utc(typical_wake_local)

        return False, None

    # -------------------------------------------------------------------------
    # Main run
    # -------------------------------------------------------------------------

    def run(self, ctx: StepContext) -> StepResult:
        from app.assistant.pipelines.dayflow.sleep.sleep_resource_generator import compute_sleep_data

        now_utc = ctx.now_utc
        now_local = ctx.now_local

        data = compute_sleep_data(now_utc=now_utc, now_local=now_local)

        user_is_up, day_start_utc = self._check_user_is_up(ctx)
        data["user_is_up"] = bool(user_is_up)
        data["day_started"] = bool(user_is_up)  # alias

        if day_start_utc:
            day_start_utc = day_start_utc.astimezone(timezone.utc)
            day_start_local = utc_to_local(day_start_utc)
            data["day_start_time"] = day_start_local.strftime("%Y-%m-%d %I:%M %p %Z")
            data["day_start_time_utc"] = day_start_utc.isoformat()
            data["wake_time"] = day_start_local.strftime("%H:%M")
            data["wake_time_today"] = day_start_local.strftime("%Y-%m-%d %H:%M")
            data["wake_time_today_local"] = day_start_local.strftime("%Y-%m-%d %I:%M %p %Z")
        else:
            data["day_start_time"] = None
            data["day_start_time_utc"] = None

        data["last_updated"] = now_local.strftime("%Y-%m-%d %I:%M %p")
        data["last_updated_utc"] = now_utc.isoformat()

        ctx.write_resource(self._output_filename(), data)

        tz = get_local_timezone()
        segments_output: Dict[str, Any] = {
            "date": now_local.strftime("%Y-%m-%d"),
            "timezone": str(getattr(tz, "key", tz)),
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated_utc": now_utc.isoformat(),
            "sleep_periods": data.get("sleep_periods", []) or [],
        }
        ctx.write_resource("resource_sleep_segments_output.json", segments_output)

        snapshot_written = False
        try:
            snapshot_written = self._maybe_write_pre_sleep_snapshot(ctx, data)
        except Exception as e:
            logger.warning("SleepStep: failed to write pre-sleep snapshot: %s", e)

        logger.info(
            "SleepStep: %s min sleep, quality=%s, user_is_up=%s, day_start=%s",
            float(data.get("total_sleep_minutes", 0) or 0),
            data.get("sleep_quality", "unknown"),
            user_is_up,
            data.get("day_start_time"),
        )

        return StepResult(
            output=data,
            state_updates={
                "wake_time_today": data.get("wake_time_today"),
                "user_is_up": bool(user_is_up),
                "day_started": bool(user_is_up),
                "day_start_time": data.get("day_start_time"),
                "day_start_time_utc": data.get("day_start_time_utc"),
            },
            debug={
                "total_sleep_minutes": data.get("total_sleep_minutes"),
                "segment_count": data.get("segment_count"),
                "wake_time": data.get("wake_time"),
                "user_is_up": bool(user_is_up),
                "day_start_time": data.get("day_start_time"),
                "pre_sleep_snapshot_written": bool(snapshot_written),
            },
        )

    def reset(self, ctx: StepContext) -> None:
        """
        Reset sleep data at daily boundary using defaults from config.

        Note:
        - wake_time_today is "YYYY-MM-DD HH:MM" (local string) for downstream parsing.
        """
        now_utc = ctx.now_utc
        now_local = ctx.now_local

        step_config = ctx.step_config or {}
        daily_reset = step_config.get("daily_reset", {}) if isinstance(step_config, dict) else {}
        defaults = daily_reset.get("output_resource_defaults", {}) if isinstance(daily_reset, dict) else {}

        tz = get_local_timezone()
        today_str = now_local.strftime("%Y-%m-%d")

        wake_time_hhmm = str(defaults.get("wake_time_today", "07:00"))
        wake_time_today_str = f"{today_str} {wake_time_hhmm}"

        bedtime_hhmm = str(defaults.get("bedtime_previous", "22:30"))

        def _parse_hhmm(hhmm: str, fallback_h: int, fallback_m: int) -> Tuple[int, int]:
            try:
                h, m = hhmm.split(":")
                return int(h), int(m)
            except Exception:
                return fallback_h, fallback_m

        bed_hour, bed_min = _parse_hhmm(bedtime_hhmm, 22, 30)
        wake_hour, wake_min = _parse_hhmm(wake_time_hhmm, 7, 0)

        bedtime_local = datetime(
            now_local.year,
            now_local.month,
            now_local.day,
            bed_hour,
            bed_min,
            0,
            tzinfo=tz,
        ) - timedelta(days=1)

        wake_local = datetime(
            now_local.year,
            now_local.month,
            now_local.day,
            wake_hour,
            wake_min,
            0,
            tzinfo=tz,
        )

        bedtime_local_str = bedtime_local.strftime("%Y-%m-%d %I:%M %p %Z")
        wake_local_str = wake_local.strftime("%Y-%m-%d %I:%M %p %Z")

        bedtime_utc = local_to_utc(bedtime_local).astimezone(timezone.utc)
        wake_utc = local_to_utc(wake_local).astimezone(timezone.utc)

        sleep_periods = [
            {
                "start": bedtime_utc.replace(tzinfo=None).isoformat(),
                "end": wake_utc.replace(tzinfo=None).isoformat(),
                "duration_minutes": float(defaults.get("total_sleep_minutes", 510) or 510),
                "type": "main_sleep",
                "source": "reset_default",
                "start_local": bedtime_local_str,
                "end_local": wake_local_str,
            }
        ]

        default_data: Dict[str, Any] = {
            "timezone": str(getattr(tz, "key", tz)),
            "date": today_str,
            "total_sleep_minutes": float(defaults.get("total_sleep_minutes", 510) or 510),
            "last_night_sleep_minutes": float(defaults.get("last_night_sleep_minutes", 510) or 510),
            "main_sleep_minutes": float(defaults.get("main_sleep_minutes", 510) or 510),
            "sleep_quality": defaults.get("sleep_quality", "good"),
            "fragmented": bool(defaults.get("fragmented", False)),
            "segment_count": int(defaults.get("segment_count", 1) or 1),
            "total_wake_minutes": float(defaults.get("total_wake_minutes", 0) or 0),
            "time_in_bed_minutes": float(defaults.get("time_in_bed_minutes", 510) or 510),
            "sleep_periods": sleep_periods,
            "wake_time": wake_time_hhmm,
            "wake_time_today": wake_time_today_str,
            "wake_time_today_local": wake_local_str,
            "bedtime_previous": bedtime_local.strftime("%Y-%m-%d %H:%M"),
            "bedtime_previous_local": bedtime_local_str,
            "user_is_up": False,
            "day_started": False,
            "day_start_time": wake_local_str,
            "day_start_time_utc": wake_utc.isoformat(),
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated_utc": now_utc.isoformat(),
            "source_breakdown_minutes": defaults.get("source_breakdown_minutes", {}),
            "_reset_reason": "daily_boundary",
        }

        ctx.write_resource(self._output_filename(), default_data)
        logger.info("SleepStep: daily reset complete")



