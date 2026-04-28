from __future__ import annotations

"""
Sleep Config

Single responsibility:
- Load sleep tracking parameters from configs/sleep_tracking.yaml
- Provide small, safe helpers for interpreting HH:MM config fields

Notes:
- This module does NOT do any sleep math.
- Timezone handling is delegated to app.assistant.utils.time_utils.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date, time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir
from app.assistant.utils.time_utils import get_local_timezone, local_to_utc

logger = get_logger(__name__)


@dataclass(frozen=True)
class SleepQualityThresholds:
    """
    Simple grading thresholds (minutes).
    """

    good_minutes: int = 420  # 7h
    fair_minutes: int = 360  # 6h


class SleepConfig:
    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            self._config_path = get_configs_dir() / "sleep_tracking.yaml"
        else:
            self._config_path = config_path
        # Per-instance cache: avoids repeated stat()/load_config() calls within
        # a single pipeline tick when multiple properties are accessed in sequence.
        self._cached_cfg: Optional[Dict[str, Any]] = None
        self._cached_mtime: Optional[float] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, force_reload: bool = False) -> Dict[str, Any]:
        """
        Loads YAML into a dict, cached first by instance mtime then by load_config.

        The per-instance cache means that accessing N properties in a single pipeline
        tick only pays one stat() + one load_config() call, not N.
        """
        from app.assistant.utils.config_loader import load_config, config_cache

        if not self._config_path.exists():
            raise FileNotFoundError(f"Sleep config file not found: {self._config_path}")

        if force_reload:
            config_cache.pop(str(self._config_path), None)
            self._cached_cfg = None
            self._cached_mtime = None

        current_mtime = self._config_path.stat().st_mtime
        if not force_reload and self._cached_cfg is not None and self._cached_mtime == current_mtime:
            return self._cached_cfg

        cfg = load_config(str(self._config_path))
        if not isinstance(cfg, dict):
            cfg = {}

        self._cached_cfg = cfg
        self._cached_mtime = current_mtime
        return cfg

    def get(self, *keys: str, default: Any = None) -> Any:
        cfg = self.load()
        cur: Any = cfg
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    # ------------------------------------------------------------------
    # Core parameters we actually use
    # ------------------------------------------------------------------

    def sleep_window_start_hhmm(self) -> str:
        return str(self.get("sleep_window", "start", default="22:30"))

    def sleep_window_end_hhmm(self) -> str:
        return str(self.get("sleep_window", "end", default="09:00"))

    @property
    def min_sleep_afk_minutes(self) -> int:
        v = self.get("min_sleep_afk_minutes", default=60)
        try:
            return max(0, int(v))
        except Exception:
            return 60

    def daily_reset_hour_local(self) -> int:
        v = self.get("daily_reset", "hour", default=5)
        try:
            return max(0, min(23, int(v)))
        except Exception:
            return 5

    def sleep_awake_divider_hhmm(self) -> str:
        """
        Divider between "AFK can count as sleep" vs "AFK counts as awake".

        Preference order:
        1) sleep_awake_divider
        2) daily_reset.hour (converted to HH:00)
        """
        v = self.get("sleep_awake_divider", default=None)
        if v:
            return str(v)

        return f"{self.daily_reset_hour_local():02d}:00"

    def quality_thresholds(self) -> SleepQualityThresholds:
        cfg = self.get("sleep_quality_thresholds", default={}) or {}
        if not isinstance(cfg, dict):
            return SleepQualityThresholds()

        def _int(name: str, default: int) -> int:
            try:
                return int(cfg.get(name, default))
            except Exception:
                return default

        good = _int("good_minutes", 420)
        fair = _int("fair_minutes", 360)
        return SleepQualityThresholds(good_minutes=good, fair_minutes=fair)

    # ------------------------------------------------------------------
    # Property accessors for time objects (used by sleep_resource_generator)
    # ------------------------------------------------------------------

    @property
    def sleep_window_start(self) -> time:
        h, m = self.parse_hhmm(self.sleep_window_start_hhmm())
        return time(h, m)

    @property
    def sleep_window_end(self) -> time:
        h, m = self.parse_hhmm(self.sleep_window_end_hhmm())
        return time(h, m)

    @property
    def sleep_awake_divider(self) -> time:
        h, m = self.parse_hhmm(self.sleep_awake_divider_hhmm())
        return time(h, m)

    @property
    def good_min_minutes(self) -> int:
        return self.quality_thresholds().good_minutes

    @property
    def fair_min_minutes(self) -> int:
        return self.quality_thresholds().fair_minutes

    # ------------------------------------------------------------------
    # Sleep segment adjustment parameters
    # ------------------------------------------------------------------

    @property
    def min_segment_hours_for_trim(self) -> float:
        v = self.get("sleep_segment_adjustments", "min_segment_hours_for_trim", default=2.0)
        try:
            return max(0.0, float(v))
        except Exception:
            return 2.0

    @property
    def start_trim_minutes(self) -> float:
        v = self.get("sleep_segment_adjustments", "start_trim_minutes", default=20)
        try:
            return max(0.0, float(v))
        except Exception:
            return 20.0

    @property
    def end_trim_minutes(self) -> float:
        v = self.get("sleep_segment_adjustments", "end_trim_minutes", default=15)
        try:
            return max(0.0, float(v))
        except Exception:
            return 15.0

    @property
    def max_trim_percent(self) -> float:
        v = self.get("sleep_segment_adjustments", "max_trim_percent", default=25)
        try:
            return max(0.0, min(100.0, float(v)))
        except Exception:
            return 25.0

    # ------------------------------------------------------------------
    # Tiny time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_hhmm(value: str) -> Tuple[int, int]:
        text = (value or "").strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid HH:MM: {value}")
        h = int(parts[0])
        m = int(parts[1])
        if h < 0 or h > 23 or m < 0 or m > 59:
            raise ValueError(f"Invalid HH:MM: {value}")
        return h, m

    def local_night_window_utc(self, anchor_local_date: date) -> Tuple[datetime, datetime]:
        """
        Returns the UTC bounds for the "night sleep inference window":
        [sleep_window.start -> sleep_awake_divider] in local time.
        """
        tz = get_local_timezone()

        sh, sm = self.parse_hhmm(self.sleep_window_start_hhmm())
        dh, dm = self.parse_hhmm(self.sleep_awake_divider_hhmm())

        # Start is previous local day at sleep_window.start
        start_local = datetime(
            year=anchor_local_date.year,
            month=anchor_local_date.month,
            day=anchor_local_date.day,
            hour=sh,
            minute=sm,
            tzinfo=tz,
        ) - timedelta(days=1)

        # End is anchor local day at divider
        end_local = datetime(
            year=anchor_local_date.year,
            month=anchor_local_date.month,
            day=anchor_local_date.day,
            hour=dh,
            minute=dm,
            tzinfo=tz,
        )

        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        return start_utc, end_utc

    def local_time_to_utc_for_boundary_day(self, boundary_date_local: str, hhmm: str) -> datetime:
        """
        Convert a HH:MM local time into UTC for the given boundary local date.
        """
        y, m, d = (int(x) for x in boundary_date_local.split("-"))
        h, mm = self.parse_hhmm(hhmm)
        tz = get_local_timezone()
        local_dt = datetime(y, m, d, h, mm, 0, tzinfo=tz)
        return local_to_utc(local_dt)


# Singleton accessor (optional)
_sleep_config: Optional[SleepConfig] = None


def get_sleep_config() -> SleepConfig:
    global _sleep_config
    if _sleep_config is None:
        _sleep_config = SleepConfig()
    return _sleep_config

