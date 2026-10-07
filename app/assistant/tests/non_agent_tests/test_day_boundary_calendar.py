"""The day's calendar is the boundary day's: 5 AM to 5 AM, not the calendar date (2026-10-07).

At 1 AM on the 7th the day being decided is the 6th. The expected calendar for the 6th held the 7th's morning events
stamped as the 6th's, and the day's tasks made heads-ups for them under the 6th."""
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.pipelines import context as day_context
from app.assistant.pipelines.dayflow.utils import context_sources
from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import normalize_expected_schedule_to_utc
from app.assistant.utils.time_utils import get_local_timezone


@pytest.fixture(autouse=True)
def boundary_at_five(monkeypatch):
    monkeypatch.setattr(day_context, "_boundary_hour_local", lambda: 5)


def _local(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=get_local_timezone()).astimezone(timezone.utc)


def test_a_clock_time_before_the_boundary_hour_is_on_the_next_calendar_date():
    out = normalize_expected_schedule_to_utc(
        [{"title": "Lights off", "start_local": "01:00 AM", "end_local": "01:15 AM"},
         {"title": "Take out the dogs", "start_local": "08:00 AM", "end_local": "08:30 AM"},
         {"title": "Late show", "start_local": "11:00 PM", "end_local": "01:00 AM"}],
        boundary_date_local="2026-10-06")
    assert out[0]["start_utc"] == _local(2026, 10, 7, 1).isoformat()
    assert out[0]["end_utc"] == _local(2026, 10, 7, 1, 15).isoformat()
    assert out[1]["start_utc"] == _local(2026, 10, 6, 8).isoformat()
    assert out[2]["start_utc"] == _local(2026, 10, 6, 23).isoformat()
    assert out[2]["end_utc"] == _local(2026, 10, 7, 1).isoformat()


def test_the_days_events_are_those_in_its_window_not_its_calendar_date(monkeypatch):
    def event(name, start, end):
        return {"id": name, "summary": name, "start_utc": start, "end_utc": end, "attendees": [], "updated": ""}
    monkeypatch.setattr(context_sources, "_load_calendar_events", lambda: [
        event("the 5th's night", _local(2026, 10, 6, 2), _local(2026, 10, 6, 3)),
        event("ends at the boundary", _local(2026, 10, 6, 4), _local(2026, 10, 6, 5)),
        event("morning walk", _local(2026, 10, 6, 8), _local(2026, 10, 6, 8, 30)),
        event("after midnight", _local(2026, 10, 7, 1), _local(2026, 10, 7, 2)),
        event("the 7th's walk", _local(2026, 10, 7, 8), _local(2026, 10, 7, 8, 30)),
    ])
    titles = [e["title"] for e in context_sources.get_calendar_events_structured_for_day("2026-10-06")]
    assert titles == ["morning walk", "after midnight"]


def test_the_tracker_sees_only_events_before_its_day_ends(monkeypatch):
    """At 1 AM on the 7th the upcoming events the tracker reads stop at 5 AM: the 7th's 8 AM walk is not the 6th's."""
    now = _local(2026, 10, 7, 1)
    seen = {}

    class Ctx:
        now_utc = now
        now_local = now.astimezone(get_local_timezone())
        state = {"boundary_date_local": "2026-10-06", "day_start_time_utc": _local(2026, 10, 6, 5).isoformat()}
        step_config = {}
        pipeline_config = {}
        resources_dir = None

        def read_resource(self, name):
            return {"date": "2026-10-06", "expected_schedule": [{"title": "x", "start_local": "08:00 AM"}],
                    "day_theme": "t"}

        def write_resource(self, name, data):
            pass

    from app.assistant.pipelines.dayflow.steps import daily_context_generator_stage as stage
    monkeypatch.setattr(stage, "get_calendar_events_upcoming_for_daily_context",
                        lambda hours: seen.setdefault("hours", hours) and [])
    for name in ("get_calendar_events_completed", "get_responded_tickets_categorized", "get_current_presence_status",
                 "get_significant_activity_segments", "get_calendar_events_structured_for_day"):
        monkeypatch.setattr(stage, name, lambda *a, **k: [])
    monkeypatch.setattr(stage, "get_desktop_activity_recent", lambda: "")
    monkeypatch.setattr(stage, "resolve_room_scope", lambda **k: {"room_id": "dayflow_orchestrator"})
    step = stage.DailyContextGeneratorStep()
    monkeypatch.setattr(step, "_get_recent_chat_excerpts", lambda ctx, hours: [])
    monkeypatch.setattr(step, "_call_agent", lambda agent_input: seen.setdefault("input", agent_input) and None)
    step.run(Ctx())
    assert seen["hours"] == pytest.approx(4.0)
    assert seen["input"]["day_of_week"] == "Tuesday"
