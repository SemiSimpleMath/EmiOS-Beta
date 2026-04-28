"""
Tests for CalendarTool.handle_get_calendar_events — specifically that the
``content`` field returned to agents is actually useful.

Regression context: before, ``content`` was just a count — e.g. "Found 109
calendar events". Agents only see ``content`` in their turn-over-turn tool
history, so they couldn't read event titles, locations, or descriptions and
kept retrying queries hoping for different results. The fix surfaces per-
event detail (title + time + location + description snippet) inline so a
planner can answer "what's the Zoom link for Friday Night Meats?" in one
call. These tests lock that invariant in.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.test.tool_tests._helpers import build_calendar_tool


def _canned_events(events_raw):
    """Wrap a list of Google-Calendar-shaped event dicts so the handler
    sees them as the return of ``get_events(...)``.

    The caller supplies the raw event fields (summary, description, location,
    start, end, id). The handler passes each through ``normalize_start_end``
    which expects Google's usual dateTime structure.
    """
    return events_raw


def _install_stub_get_events(monkeypatch, events):
    from app.assistant.lib.core_tools.calendar_tool import calendar_tool as ct
    def _stub(**_kwargs):
        return events
    monkeypatch.setattr(ct, "get_events", _stub, raising=True)


def _base_event(*, event_id, summary, description="", location="", start_iso="2026-04-24T20:00:00Z", end_iso="2026-04-24T22:00:00Z", recurring_event_id=None):
    """Minimal Google Calendar event shape that passes handler parsing."""
    event = {
        "id": event_id,
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
    }
    if recurring_event_id:
        event["recurringEventId"] = recurring_event_id
    return event


def test_content_includes_event_title_and_time(monkeypatch):
    """A single event's title and start time must appear in ``content``."""
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(
            event_id="evt1",
            summary="Friday Night Meats",
            start_iso="2026-04-24T20:00:00Z",
            end_iso="2026-04-24T22:00:00Z",
        ),
    ])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],  # skip calendarList() API resolve
        "single_events": True,
    })

    assert result.result_type == "calendar_events"
    content = result.content or ""
    assert "Friday Night Meats" in content, (
        f"Event title missing from content. content was:\n{content}"
    )
    # Some timestamp reference should be present for the event.
    assert "2026-04-24" in content, (
        f"Event date missing from content. content was:\n{content}"
    )


def test_content_surfaces_zoom_link_from_description(monkeypatch):
    """A Zoom URL in an event's description must appear in ``content``.

    This is the exact scenario that caused the audit failure: the agent
    needed the Zoom link for a recurring Friday event. The link lives in
    the event's description field in Google Calendar.
    """
    zoom_url = "https://us02web.zoom.us/j/83742000488?pwd=abc123"
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(
            event_id="evt1",
            summary="Friday Night Meats",
            description=(
                "Weekly hangout.\n"
                f"Zoom link: {zoom_url}\n"
                "Meeting ID: 837 4200 0488"
            ),
        ),
    ])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
    })
    content = result.content or ""
    assert zoom_url in content, (
        f"Zoom link from description missing from content. content was:\n{content}"
    )


def test_content_surfaces_location_field(monkeypatch):
    """Event location should appear in ``content`` when present."""
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(
            event_id="evt1",
            summary="Coffee with Martin",
            location="Blue Bottle Coffee, Irvine",
        ),
    ])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
    })
    content = result.content or ""
    assert "Blue Bottle Coffee" in content, (
        f"Location missing from content. content was:\n{content}"
    )


def test_content_dedupes_recurring_instances_by_title(monkeypatch):
    """Multiple instances of the same recurring event title should appear
    once in the summary. A planner asked 'when is Friday Night Meats?'
    doesn't need to scroll past 10 identical rows.
    """
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="evt1", summary="Friday Night Meats",
                    start_iso="2026-04-17T20:00:00Z", end_iso="2026-04-17T22:00:00Z",
                    recurring_event_id="master"),
        _base_event(event_id="evt2", summary="Friday Night Meats",
                    start_iso="2026-04-24T20:00:00Z", end_iso="2026-04-24T22:00:00Z",
                    recurring_event_id="master"),
        _base_event(event_id="evt3", summary="Friday Night Meats",
                    start_iso="2026-05-01T20:00:00Z", end_iso="2026-05-01T22:00:00Z",
                    recurring_event_id="master"),
    ])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-17T00:00:00Z",
        "end_date": "2026-05-01T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
    })
    content = result.content or ""
    # Count occurrences of the title in the per-event lines (not in the header
    # which may mention counts, not titles).
    title_count = content.count("- Friday Night Meats")
    assert title_count == 1, (
        f"Expected 1 line for the recurring title; got {title_count}. content was:\n{content}"
    )
    # But the header should still reflect the full count (3 recurring).
    assert "Found 3" in content or "3 recurring" in content, (
        f"Header should indicate the underlying count. content was:\n{content}"
    )


def test_content_lists_multiple_distinct_events(monkeypatch):
    """Multiple unique-title events all appear in content with their titles."""
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="a", summary="Take out the dogs"),
        _base_event(event_id="b", summary="Analytics team standup"),
        _base_event(event_id="c", summary="Friday Night Meats"),
    ])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
    })
    content = result.content or ""
    assert "Take out the dogs" in content
    assert "Analytics team standup" in content
    assert "Friday Night Meats" in content


def test_content_is_empty_summary_on_zero_events(monkeypatch):
    """Zero events still produces a parseable content string (count=0 header)."""
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
    })
    content = result.content or ""
    assert "Found 0 calendar event" in content


def test_event_name_contains_filters_by_substring(monkeypatch):
    """``event_name_contains`` should narrow the result set to matching titles
    (case-insensitive substring). This is the lightweight alternative to the
    disabled semantic-similarity tool — covers 'find the X event' without
    needing sentence-transformers.
    """
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="a", summary="Take out the dogs"),
        _base_event(event_id="b", summary="Analytics team standup"),
        _base_event(event_id="c", summary="Friday Night Meats",
                    description="Zoom: https://us02web.zoom.us/j/123"),
        _base_event(event_id="d", summary="Walk the dogs"),
    ])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
        "event_name_contains": "friday night",
    })
    assert isinstance(result.data_list, list)
    assert len(result.data_list) == 1, (
        f"Expected exactly 1 match for 'friday night'; got {len(result.data_list)}: "
        f"{[e.get('summary') for e in result.data_list]}"
    )
    assert result.data_list[0]["summary"] == "Friday Night Meats"
    content = result.content or ""
    assert "Friday Night Meats" in content


def test_event_name_contains_is_case_insensitive(monkeypatch):
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="a", summary="FRIDAY NIGHT MEATS"),
    ])
    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
        "event_name_contains": "friday",
    })
    assert len(result.data_list) == 1


def test_event_name_contains_empty_match_returns_zero(monkeypatch):
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="a", summary="Take out the dogs"),
    ])
    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
        "event_name_contains": "nothing matches this",
    })
    assert len(result.data_list) == 0
    assert "Found 0 calendar event" in (result.content or "")


def test_data_list_still_populated_with_full_event_objects(monkeypatch):
    """``data_list`` should continue carrying the full structured events even
    with the richer ``content`` — downstream code that was reading data_list
    must not be regressed.
    """
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="evt1", summary="Friday Night Meats",
                    description="Zoom link: https://us02web.zoom.us/j/123"),
    ])

    result = tool.handle_get_calendar_events({
        "start_date": "2026-04-24T00:00:00Z",
        "end_date": "2026-04-24T23:59:59Z",
        "calendar_names": ["primary"],
        "single_events": True,
    })
    assert isinstance(result.data_list, list)
    assert len(result.data_list) == 1
    event = result.data_list[0]
    assert event["summary"] == "Friday Night Meats"
    assert "us02web.zoom.us" in event["description"]
    assert event["id"] == "evt1"
