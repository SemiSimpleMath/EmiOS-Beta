"""Auto-recovery probe gating for auto-disabled routines.

The fetcher routines opted into on_error.auto_retry_after_seconds
(2026-07-08 — an OpenWeatherMap slowdown auto-disabled fetch_weather
permanently until a manual toggle). This pins _is_probe_due's contract:
probes fire only for AUTO-disabled routines that opted in, after the
configured interval; user-toggled disables never auto-recover.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.routine_manager.routine_manager import (
    RoutineConfig,
    RoutineManager,
    RoutineRunState,
)


def _mgr() -> RoutineManager:
    return RoutineManager.__new__(RoutineManager)


def _routine(retry_after: int) -> RoutineConfig:
    return RoutineConfig(
        routine_id="fetch_probe_test",
        enabled=True,
        run_policy={"type": "interval", "min_interval_seconds": 660},
        on_error={"auto_retry_after_seconds": retry_after},
    )


def _state(*, reason=None, disabled_at=None) -> RoutineRunState:
    entry = RoutineRunState()
    entry.auto_disabled_reason = reason
    entry.auto_disabled_at_utc = disabled_at
    return entry


NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def test_user_toggled_disable_never_probes():
    # No auto_disabled_reason = the user turned it off; leave it off.
    entry = _state(reason=None, disabled_at=None)
    assert _mgr()._is_probe_due(_routine(1800), entry, NOW) is False


def test_no_opt_in_never_probes():
    entry = _state(reason="3 straight timeouts", disabled_at=(NOW - timedelta(hours=2)).isoformat())
    assert _mgr()._is_probe_due(_routine(0), entry, NOW) is False


def test_probe_fires_after_interval():
    entry = _state(reason="3 straight timeouts", disabled_at=(NOW - timedelta(minutes=31)).isoformat())
    assert _mgr()._is_probe_due(_routine(1800), entry, NOW) is True


def test_probe_waits_for_interval():
    entry = _state(reason="3 straight timeouts", disabled_at=(NOW - timedelta(minutes=10)).isoformat())
    assert _mgr()._is_probe_due(_routine(1800), entry, NOW) is False


def test_missing_timestamp_allows_first_probe():
    # Odd state (disabled without a stamp) must not wedge the routine.
    entry = _state(reason="3 straight timeouts", disabled_at=None)
    assert _mgr()._is_probe_due(_routine(1800), entry, NOW) is True


def test_fetcher_configs_opted_in():
    import glob
    import json

    opted = {}
    for path in glob.glob("configs/routines/public/fetch_*.json") + [
        "configs/routines/public/location_refresh.json"
    ]:
        if path.endswith(".compiled.json"):
            continue
        cfg = json.load(open(path, encoding="utf-8"))
        rid = cfg.get("id")
        if not rid:
            continue
        opted[rid] = int((cfg.get("on_error") or {}).get("auto_retry_after_seconds") or 0)
    assert opted, "no fetcher configs found — wrong working directory?"
    lazy = [rid for rid, secs in opted.items() if secs <= 0]
    assert not lazy, f"fetcher routines without auto-recovery probe: {lazy}"
