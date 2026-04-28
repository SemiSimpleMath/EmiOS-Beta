"""
Contract tests for delete_repeating_calendar_event.

This is the destructive sibling of delete_calendar_event. It MUST only act on
recurring series, and it must correctly resolve to the base series id whether
the caller passed an instance id or the base id directly.
"""
import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.test.tool_tests._helpers import build_calendar_tool


def test_delete_series_by_base_id_deletes_base_id(monkeypatch):
    tool, fake = build_calendar_tool(monkeypatch)
    base_id = "qgstjk55ji1tlbmm35hp59tcn8"
    fake.set_event(base_id, {"id": base_id, "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=FR"]})

    result = tool.handle_delete_repeating_calendar_event({"event_id": base_id})

    assert result.result_type == "success"
    assert fake.last_delete_target() == base_id
    # We had to fetch once to confirm it's a series.
    gets = fake.get_calls_of("get")
    assert len(gets) == 1 and gets[0].event_id == base_id


def test_delete_series_by_instance_id_resolves_to_parent(monkeypatch):
    tool, fake = build_calendar_tool(monkeypatch)
    instance_id = "qgstjk55ji1tlbmm35hp59tcn8_20260411T030000Z"
    parent_id = "qgstjk55ji1tlbmm35hp59tcn8"
    fake.set_event(instance_id, {"id": instance_id, "recurringEventId": parent_id})

    result = tool.handle_delete_repeating_calendar_event({"event_id": instance_id})

    assert result.result_type == "success"
    # The actual delete must target the parent, not the instance.
    assert fake.last_delete_target() == parent_id


def test_delete_series_rejects_non_recurring_event(monkeypatch):
    """A non-recurring event has neither recurringEventId nor a recurrence field."""
    tool, fake = build_calendar_tool(monkeypatch)
    fake.set_event("abc", {"id": "abc"})

    result = tool.handle_delete_repeating_calendar_event({"event_id": "abc"})

    assert result.result_type == "error", (
        "Regression: delete_repeating_calendar_event deleted a non-recurring event — "
        "the tool should have rejected and pointed the caller at delete_calendar_event."
    )
    # Must NOT have issued a delete.
    assert fake.get_calls_of("delete") == []


def test_delete_series_requires_event_id(monkeypatch):
    tool, fake = build_calendar_tool(monkeypatch)
    result = tool.handle_delete_repeating_calendar_event({})
    assert result.result_type == "error"
