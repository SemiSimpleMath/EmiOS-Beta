"""The situation snapshot's calendar block shows what is ON now, not only what starts later.

2026-09-18 09:17: the block said "No upcoming events in next 8 hours" two minutes into a standup,
beside a schedule block that listed the standup — and the situation auditor filed the contradiction.
The query listed events that START in the window; a meeting stops being "upcoming" the moment it
begins, which is exactly when it matters.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.pipelines.dayflow.utils import context_sources
from app.assistant.pipelines.dayflow.utils.situation_snapshot import _build_calendar


def _events(now):
    return [
        {"summary": "Analytics team standup meeting", "start_utc": now - timedelta(minutes=2),
         "end_utc": now + timedelta(minutes=58)},
        {"summary": "Work Hours", "start_utc": now - timedelta(minutes=17),
         "end_utc": now + timedelta(hours=7)},
        {"summary": "Family time", "start_utc": now + timedelta(hours=8, minutes=43),
         "end_utc": now + timedelta(hours=10, minutes=43)},
        {"summary": "Yesterday's thing", "start_utc": now - timedelta(days=1),
         "end_utc": now - timedelta(days=1) + timedelta(hours=1)},
    ]


def test_an_in_progress_meeting_is_shown_and_labelled(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(context_sources, "_load_calendar_events", lambda: _events(now))
    block = _build_calendar(now)
    assert "Analytics team standup meeting (in progress)" in block
    assert "Work Hours (in progress)" in block
    assert "Family time" not in block, "starts after the window"
    assert "Yesterday" not in block
    assert "No upcoming events" not in block


def test_the_empty_case_says_now_as_well_as_later(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(context_sources, "_load_calendar_events", lambda: [])
    assert _build_calendar(now) == "### Calendar\nNothing on the calendar now or in the next 8 hours."


def test_the_starts_only_query_is_unchanged_for_its_own_callers(monkeypatch):
    """cron tickets and the DJ want "what starts soon"; they keep that."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(context_sources, "_load_calendar_events", lambda: _events(now))
    names = [e["event_name"] for e in context_sources.get_calendar_events_for_orchestrator(hours=8)]
    assert names == []
