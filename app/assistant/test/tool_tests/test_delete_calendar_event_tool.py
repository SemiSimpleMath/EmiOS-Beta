"""
Contract tests for delete_calendar_event.

Anchor invariant: this tool NEVER deletes an entire recurring series. It
deletes exactly one thing — either a non-recurring event or a single
occurrence of a recurring series. If someone reintroduces auto-escalation
(the April 10 Friday Night Meats bug), these tests go red.
"""
import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.test.tool_tests._helpers import (
    build_calendar_tool,
    make_tool_message,
)


def test_delete_single_non_recurring_event_passes_event_id_unchanged(monkeypatch):
    tool, fake = build_calendar_tool(monkeypatch)
    fake.set_event("abc123", {"id": "abc123"})

    result = tool.handle_delete_calendar_event({"event_id": "abc123"})

    assert result.result_type == "success"
    # Must have issued exactly one delete, targeting the id we passed.
    deletes = fake.get_calls_of("delete")
    assert len(deletes) == 1
    assert deletes[0].event_id == "abc123"
    # Single-event path must not issue any get()-to-check-recurrence calls.
    assert fake.get_calls_of("get") == []


def test_delete_single_instance_of_recurring_series_targets_the_instance_not_the_parent(monkeypatch):
    """
    The regression target. Before the fix, this tool would inspect
    recurringEventId on the instance and silently delete the parent series.
    """
    tool, fake = build_calendar_tool(monkeypatch)
    instance_id = "qgstjk55ji1tlbmm35hp59tcn8_20260411T030000Z"
    parent_id = "qgstjk55ji1tlbmm35hp59tcn8"
    fake.set_event(instance_id, {"id": instance_id, "recurringEventId": parent_id})

    result = tool.handle_delete_calendar_event({"event_id": instance_id})

    assert result.result_type == "success"
    # Must target the instance — NEVER the parent.
    deletes = fake.get_calls_of("delete")
    assert len(deletes) == 1, f"expected exactly one delete, got {deletes}"
    assert deletes[0].event_id == instance_id
    assert deletes[0].event_id != parent_id, (
        "Regression: delete_calendar_event escalated to the parent recurring series — "
        "this is the Friday Night Meats bug."
    )


def test_delete_requires_event_id(monkeypatch):
    tool, fake = build_calendar_tool(monkeypatch)
    result = tool.handle_delete_calendar_event({})
    assert result.result_type == "error"


def test_delete_returns_error_on_api_failure(monkeypatch):
    """If the Google API raises, the handler should surface a tool error, not crash."""
    tool, fake = build_calendar_tool(monkeypatch)

    class _BoomRequest:
        def execute(self):
            raise RuntimeError("simulated API failure")

    class _BoomEvents:
        def delete(self, *, calendarId, eventId):
            return _BoomRequest()

    class _BoomService:
        def events(self):
            return _BoomEvents()

    tool.service = _BoomService()
    result = tool.handle_delete_calendar_event({"event_id": "abc"})
    assert result.result_type == "error"


def test_scope_argument_is_ignored_if_someone_passes_it(monkeypatch):
    """
    The old design had a scope='single'|'all' flag. It was removed to prevent
    the 'forgot to set scope' class of bugs. If a planner still sends it,
    the tool should ignore it and do the single-event thing.
    """
    tool, fake = build_calendar_tool(monkeypatch)
    instance_id = "abc_20260411T030000Z"
    fake.set_event(instance_id, {"id": instance_id, "recurringEventId": "abc"})

    # Deliberately pass scope='all' — must NOT escalate to parent.
    result = tool.handle_delete_calendar_event({"event_id": instance_id, "scope": "all"})

    assert result.result_type == "success"
    assert fake.last_delete_target() == instance_id
