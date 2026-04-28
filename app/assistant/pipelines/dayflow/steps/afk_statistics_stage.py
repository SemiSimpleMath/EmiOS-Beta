from __future__ import annotations

"""
AFK Statistics Step

Flow:
1. Load step config, get last run timestamp (cursor)
2. Prepare inputs: Get day_start_time_utc from state (or fallback to boundary)
3. Execute: Request AFK stats from afk_manager since day_start
4. Output: Write resource_afk_statistics_output.json

Notes:
- Realtime snapshot comes only from DI.afk_monitor (if available).
- This stage does not treat the output resource as an input fallback, because the
  resource schema is not the same as AFKMonitor's snapshot schema.
- Output includes both LOCAL display strings and UTC ISO strings for safe downstream math.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_to_local, local_to_utc, get_local_timezone, parse_iso_utc
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult

logger = get_logger(__name__)


class AFKStatisticsStep(BaseStep):
    """
    Pipeline step for AFK statistics.

    Combines:
    - Real-time AFK status from DI.afk_monitor (best-effort)
    - Daily aggregates (_today: since boundary)
    - Since-wake aggregates (_since_wake: since day_start_time_utc from sleep stage)
    """

    step_id: str = "afk_statistics"

    # -------------------------------------------------------------------------
    # Config & Cursor
    # -------------------------------------------------------------------------

    def _output_filename(self) -> str:
        return "resource_afk_statistics_output.json"

    def _get_last_run_utc(self, ctx: StepContext) -> Optional[datetime]:
        step_runs = ctx.state.get("step_runs", {})
        step_info = step_runs.get(self.step_id, {}) or {}
        stage_info = step_info  # legacy variable name in downstream code
        return parse_iso_utc(stage_info.get("last_run_utc"))

    def _get_afk_snapshot(self) -> Dict[str, Any]:
        """
        Best-effort realtime snapshot from AFKMonitor.

        Important:
        - No fallback to resource_afk_statistics_output.json, because that is an output
          artifact and does not share the AFKMonitor snapshot schema.
        """
        try:
            from app.assistant.ServiceLocator.service_locator import DI

            monitor = getattr(DI, "afk_monitor", None)
            if monitor is None:
                return {}

            snapshot = monitor.get_computer_activity()
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception:
            return {}

    # -------------------------------------------------------------------------
    # Time Helpers
    # -------------------------------------------------------------------------

    def _get_boundary_start_utc(self, ctx: StepContext) -> datetime:
        """
        Get boundary hour local (default 5AM) as UTC.
        """
        step_cfg = ctx.step_config or {}
        daily_reset = step_cfg.get("daily_reset", {}) if isinstance(step_cfg, dict) else {}
        boundary_hour = int(daily_reset.get("boundary_hour_local", 5))

        now_local = ctx.now_local
        tz = get_local_timezone()

        if now_local.hour < boundary_hour:
            boundary_date = now_local.date() - timedelta(days=1)
        else:
            boundary_date = now_local.date()

        boundary_local = datetime(
            boundary_date.year,
            boundary_date.month,
            boundary_date.day,
            boundary_hour,
            0,
            0,
            tzinfo=tz,
        )
        return local_to_utc(boundary_local)

    def _get_day_start_utc(self, ctx: StepContext) -> datetime:
        """
        Get the effective day start time:
        1) day_start_time_utc from state (when user first became active)
        2) Fallback to boundary time
        """
        day_start_str = ctx.state.get("day_start_time_utc")
        day_start_utc = parse_iso_utc(day_start_str)
        if day_start_utc:
            return day_start_utc
        return self._get_boundary_start_utc(ctx)

    # -------------------------------------------------------------------------
    # Gate Logic
    # -------------------------------------------------------------------------

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        return True, "ready"

    # -------------------------------------------------------------------------
    # Main Run
    # -------------------------------------------------------------------------

    def run(self, ctx: StepContext) -> StepResult:
        """
        1. Compute boundary_utc and day_start_utc
        2. Best-effort realtime overlay from AFKMonitor
        3. Request AFK stats from afk_manager for today and since_wake
        4. Write output resource
        """
        from app.assistant.afk_manager.afk_statistics import get_afk_statistics

        now_utc = ctx.now_utc
        now_local = ctx.now_local

        boundary_utc = self._get_boundary_start_utc(ctx)
        day_start_utc = self._get_day_start_utc(ctx)

        logger.debug(
            f"AFKStatisticsStage: boundary_utc={boundary_utc.isoformat()}, "
            f"day_start_utc={day_start_utc.isoformat()}"
        )

        # Realtime snapshot (optional)
        realtime = self._get_afk_snapshot()

        # If monitor missing, treat realtime as unknown and let DB stats stand alone
        has_realtime = bool(realtime)

        # is_currently_active and current_active_start_utc are only meaningful with monitor data
        # Default behavior when missing: treat as not currently active, and pass None start.
        is_currently_active = False
        current_active_start_utc: Optional[datetime] = None

        if has_realtime:
            # Snapshot schema: is_afk bool and active_start_utc ISO string (while active)
            is_afk = bool(realtime.get("is_afk", False))
            is_currently_active = not is_afk
            current_active_start_utc = parse_iso_utc(realtime.get("active_start_utc"))

        # DB stats since boundary (today)
        stats_today = get_afk_statistics(
            since_utc=boundary_utc,
            current_active_start_utc=current_active_start_utc,
            is_currently_active=is_currently_active,
        )

        # DB stats since day start (since_wake)
        if day_start_utc > boundary_utc:
            stats_since_wake = get_afk_statistics(
                since_utc=day_start_utc,
                current_active_start_utc=current_active_start_utc,
                is_currently_active=is_currently_active,
            )
        else:
            stats_since_wake = stats_today

        def _to_local_str(dt_utc: Optional[datetime]) -> Optional[str]:
            if dt_utc is None:
                return None
            return utc_to_local(dt_utc).strftime("%Y-%m-%d %I:%M %p")

        boundary_local = utc_to_local(boundary_utc)
        day_start_local = utc_to_local(day_start_utc)

        total_active_today = float(stats_today.get("total_active_time_minutes", 0) or 0)
        total_afk_today = float(stats_today.get("total_afk_time_minutes", 0) or 0)
        afk_count_today = int(stats_today.get("afk_count", 0) or 0)

        total_active_since_wake = float(stats_since_wake.get("total_active_time_minutes", 0) or 0)
        total_afk_since_wake = float(stats_since_wake.get("total_afk_time_minutes", 0) or 0)

        active_work_session_minutes = float(stats_today.get("active_work_session_minutes", 0) or 0)
        current_afk_minutes = float(stats_today.get("current_afk_minutes", 0) or 0)

        # Derive afk_start display from realtime snapshot if present
        afk_start_utc = parse_iso_utc(realtime.get("last_afk_start")) if has_realtime else None
        afk_start_local = _to_local_str(afk_start_utc) if afk_start_utc else None

        # active_start is already a local display string in the snapshot when active
        active_start_local = realtime.get("active_start") if has_realtime else None

        data: Dict[str, Any] = {
            # Realtime overlay (best-effort)
            "has_realtime": has_realtime,
            "is_afk": bool(realtime.get("is_afk", False)) if has_realtime else False,
            "idle_minutes": float(realtime.get("idle_minutes", 0) or 0) if has_realtime else 0.0,
            "active_start": active_start_local,
            "afk_start": afk_start_local,

            # Also include UTC counterparts when available
            "active_start_utc": current_active_start_utc.isoformat() if current_active_start_utc else None,
            "afk_start_utc": afk_start_utc.isoformat() if afk_start_utc else None,

            # Today stats (since boundary)
            "total_active_time_today": round(total_active_today, 1),
            "total_afk_time_today": round(total_afk_today, 1),
            "afk_count_today": afk_count_today,

            # Since wake stats (since day_start_time_utc)
            "total_active_time_since_wake": round(total_active_since_wake, 1),
            "total_afk_time_since_wake": round(total_afk_since_wake, 1),

            # Current session info (computed in afk_statistics)
            "active_work_session_minutes": round(active_work_session_minutes, 1),
            "current_afk_minutes": round(current_afk_minutes, 1),

            # Metadata
            "boundary_time": boundary_local.strftime("%Y-%m-%d %I:%M %p"),
            "day_start_time": day_start_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),

            # And UTC metadata for downstream math
            "boundary_time_utc": boundary_utc.isoformat(),
            "day_start_time_utc": day_start_utc.isoformat(),
            "last_updated_utc": now_utc.isoformat(),
        }

        ctx.write_resource(self._output_filename(), data)

        logger.info(
            f"AFKStatisticsStage: has_realtime={has_realtime}, is_afk={data['is_afk']}, "
            f"active_today={total_active_today:.0f}min, afk_today={total_afk_today:.0f}min"
        )

        return StepResult(
            output=data,
            debug={
                "boundary_time_local_hm": boundary_local.strftime("%H:%M"),
                "day_start_time_local_hm": day_start_local.strftime("%H:%M"),
                "has_realtime": has_realtime,
                "is_currently_active": is_currently_active,
            },
        )

    def reset(self, ctx: StepContext) -> None:
        """
        Reset AFK statistics at boundary.

        Counters are set to defaults; next run recomputes from DB segments.
        """
        now_local = ctx.now_local
        now_utc = ctx.now_utc

        boundary_utc = self._get_boundary_start_utc(ctx)
        boundary_local = utc_to_local(boundary_utc)

        step_cfg = ctx.step_config or {}
        daily_reset = step_cfg.get("daily_reset", {}) if isinstance(step_cfg, dict) else {}
        defaults = daily_reset.get("output_resource_defaults", {}) if isinstance(daily_reset, dict) else {}

        default_data: Dict[str, Any] = {
            "has_realtime": False,

            "is_afk": defaults.get("is_afk", False),
            "idle_minutes": defaults.get("idle_minutes", 0),
            "active_start": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "afk_start": None,

            "active_start_utc": None,
            "afk_start_utc": None,

            "total_active_time_today": defaults.get("total_active_time_today", 0),
            "total_afk_time_today": defaults.get("total_afk_time_today", 0),
            "afk_count_today": defaults.get("afk_count_today", 0),

            "total_active_time_since_wake": defaults.get("total_active_time_since_wake", 0),
            "total_afk_time_since_wake": defaults.get("total_afk_time_since_wake", 0),

            "active_work_session_minutes": defaults.get("active_work_session_minutes", 0),
            "current_afk_minutes": defaults.get("current_afk_minutes", 0),

            "boundary_time": boundary_local.strftime("%Y-%m-%d %I:%M %p"),
            "day_start_time": None,  # Will be set by sleep stage
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),

            "boundary_time_utc": boundary_utc.isoformat(),
            "day_start_time_utc": None,
            "last_updated_utc": now_utc.isoformat(),

            "_reset_reason": "daily_boundary",
        }

        ctx.write_resource(self._output_filename(), default_data)
        logger.info("AFKStatisticsStep: daily reset complete")



