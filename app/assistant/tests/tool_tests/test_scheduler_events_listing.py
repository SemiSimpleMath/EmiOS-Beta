"""get_scheduler_events must LIST what it found (2026-09-09).

Agents only see a tool result's `content`, never its `data_list`. The fetch
handler used to return the fixed sentence "Successfully retrieved scheduler
events." with the events trapped in data_list, so a planner could never see
which reminders existed — it re-ran the fetch 22 times in one run hunting for
a trash-night reminder. The listing now carries title, local time, id, type,
importance and message per event.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.lib.core_tools.scheduler_tool.scheduler_tool import format_scheduler_events
from app.assistant.scheduler.pydantic_types.base_event_data import BaseEventData
from app.assistant.utils.time_utils import utc_to_local


def _event(**kw) -> BaseEventData:
    base = dict(event_id="abc-123", event_type="one_time_event",
                start_date="2026-09-10T05:00:00+00:00",
                event_payload={"title": "Take out trash bins", "task_type": "reminder",
                               "importance": 3, "payload_message": "Bins to the curb"})
    base.update(kw)
    return BaseEventData(**base)


def test_listing_names_each_event_with_id_local_time_and_message():
    text = format_scheduler_events([_event()], "2026-09-09T00:00:00-07:00", "2026-09-11T23:59:59-07:00")
    lines = text.splitlines()
    assert lines[0].startswith("Found 1 scheduler event from 2026-09-09T00:00:00-07:00")
    assert "Take out trash bins" in lines[1]
    assert "id=abc-123" in lines[1]
    assert "type=one_time_event" in lines[1]
    assert "importance=3" in lines[1]
    assert "message=Bins to the curb" in lines[1]
    # Storage is UTC; prompts are local.
    assert utc_to_local("2026-09-10T05:00:00+00:00").strftime("%Y-%m-%d %H:%M") in lines[1]


def test_interval_event_shows_period_and_next_occurrence():
    ev = _event(event_id="rep-1", event_type="interval", interval=86400,
                event_payload={"title": "Walk the dogs", "occurrence": "2026-09-10T15:00:00+00:00"})
    text = format_scheduler_events([ev], "a", "b")
    line = text.splitlines()[1]
    assert "Walk the dogs" in line
    assert "every 86400s" in line
    assert utc_to_local("2026-09-10T15:00:00+00:00").strftime("%Y-%m-%d %H:%M") in line


def test_empty_window_says_so_instead_of_claiming_success():
    text = format_scheduler_events([], "2026-09-09T00:00:00-07:00", "2026-09-09T23:59:59-07:00")
    assert text == "Found 0 scheduler events from 2026-09-09T00:00:00-07:00 to 2026-09-09T23:59:59-07:00."
