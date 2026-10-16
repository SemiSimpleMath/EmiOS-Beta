"""A schedule item's status is computed from its times and the clock; the tracker reports only evidence (2026-10-15).

At 6:12 AM the context tracker marked the 4:00 PM therapy 'completed' with no signal about it, and the day's tasks
cancelled that evening's notices as past."""
from datetime import datetime, timezone

import pytest

from app.assistant.pipelines import context as day_context
from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import clock_status
from app.assistant.utils.time_utils import get_local_timezone


@pytest.fixture(autouse=True)
def boundary_at_five(monkeypatch):
    monkeypatch.setattr(day_context, "_boundary_hour_local", lambda: 5)


def _local(h, mi=0):
    return datetime(2026, 1, 8, h, mi, tzinfo=get_local_timezone()).astimezone(timezone.utc)


def test_the_clock_decides_where_an_item_stands():
    assert clock_status(_local(16), _local(17), _local(6, 12)) == "upcoming"
    assert clock_status(_local(16), _local(17), _local(16, 30)) == "ongoing"
    assert clock_status(_local(16), _local(17), _local(17, 1)) == "completed"
    assert clock_status(_local(16), None, _local(18)) == "ongoing"


def test_the_tracker_reports_evidence_and_the_clock_sets_the_rest(monkeypatch, tmp_path):
    now = _local(6, 12)
    written = {}

    class Ctx:
        now_utc = now
        now_local = now.astimezone(get_local_timezone())
        state = {"boundary_date_local": "2026-01-08", "day_start_time_utc": _local(5).isoformat()}
        step_config = {}
        pipeline_config = {}
        resources_dir = tmp_path

        def read_resource(self, name):
            return {"date": "2026-01-08", "expected_schedule": [{"title": "x", "start_local": "08:00 AM"}],
                    "day_theme": "t"}

        def write_resource(self, name, data):
            written[name] = data

    from app.assistant.pipelines.dayflow.steps import daily_context_generator_stage as stage
    for name in ("get_calendar_events_upcoming_for_daily_context", "get_calendar_events_completed",
                 "get_responded_tickets_categorized", "get_current_presence_status",
                 "get_significant_activity_segments", "get_calendar_events_structured_for_day"):
        monkeypatch.setattr(stage, name, lambda *a, **k: [])
    monkeypatch.setattr(stage, "get_desktop_activity_recent", lambda: "")
    monkeypatch.setattr(stage, "resolve_room_scope", lambda **k: {"room_id": "dayflow_orchestrator"})
    monkeypatch.setattr(stage, "project_subconscious_for_daily_context", lambda: {
        "active_concerns_this_week": [], "active_concerns_longer_horizon": [], "weekly_schedule_excerpt": {}})
    step = stage.DailyContextGeneratorStep()
    monkeypatch.setattr(step, "_get_recent_chat_excerpts", lambda ctx, hours: [])
    monkeypatch.setattr(step, "_call_agent", lambda agent_input: {
        "expected_schedule": [
            {"title": "Early check", "start_local": "5:30 AM", "end_local": "6:00 AM", "reported": "scheduled"},
            {"title": "Take out the dogs", "start_local": "8:00 AM", "end_local": "8:30 AM", "reported": "done"},
            {"title": "Therapy", "start_local": "4:00 PM", "end_local": "5:00 PM", "reported": "scheduled"},
            {"title": "Family time", "start_local": "6:00 PM", "end_local": "8:00 PM", "reported": "cancelled"}],
        "day_theme": "t", "milestones": [], "current_status": "At keyboard"})
    step.run(Ctx())
    status = {i["title"]: i["status"] for i in written["resource_daily_context_generator_output.json"]["expected_schedule"]}
    assert status == {"Early check": "completed", "Take out the dogs": "completed", "Therapy": "upcoming",
                      "Family time": "cancelled"}
    assert written["resource_expected_calendar.json"]["expected_schedule"][2]["status"] == "upcoming"
