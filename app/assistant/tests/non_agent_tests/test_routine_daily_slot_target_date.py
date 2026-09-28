"""RoutineManager._daily_slot_target_date — a daily slot names the pipeline day it stands for, whether
it fires on time or hours late. Boundary hour is read from configs/dayflow_pipeline.json (05:00 here)."""
from datetime import datetime

from app.assistant.pipelines.context import _boundary_hour_local
from app.assistant.routine_manager.routine_manager import RoutineConfig, RoutineManager


def _routine(policy):
    return RoutineConfig(routine_id="t", enabled=True, name="t", runner="pipeline", spec={}, run_policy=policy)


def test_pre_boundary_slot_names_the_day_that_just_ended_even_when_run_late():
    assert _boundary_hour_local() > 0
    r = _routine({"type": "daily", "time_local": "00:05"})
    on_time = RoutineManager._daily_slot_target_date(r, datetime(2026, 9, 28, 0, 5))
    late = RoutineManager._daily_slot_target_date(r, datetime(2026, 9, 28, 14, 38))
    assert on_time == late == "2026-09-27"


def test_daytime_slot_names_today():
    r = _routine({"type": "daily", "time_local": "08:30"})
    assert RoutineManager._daily_slot_target_date(r, datetime(2026, 9, 28, 16, 0)) == "2026-09-28"


def test_non_daily_and_malformed_policies_give_none():
    assert RoutineManager._daily_slot_target_date(_routine({"type": "interval"}), datetime(2026, 9, 28, 1, 0)) is None
    assert RoutineManager._daily_slot_target_date(_routine({"type": "daily", "time_local": "nope"}), datetime(2026, 9, 28, 1, 0)) is None
