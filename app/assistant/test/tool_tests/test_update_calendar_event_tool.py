"""
Contract tests for update_calendar_event.

update_calendar_event retains the scope='single'|'all' pattern that we removed
from delete_calendar_event. The critical invariant: whatever scope the planner
passes (including nothing) must reach edit_event UNCHANGED. No silent
defaulting to 'all', no silent escalation to the parent series — same bug
class as the Friday Night Meats delete incident.
"""
import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.test.tool_tests._helpers import build_calendar_tool


def _install_edit_event_spy(monkeypatch):
    """
    Replace edit_event (and the downstream event normalize/repo mappers) with
    spies so tests can assert on what handle_update_calendar_event passed in
    without needing a real Google response shape.
    """
    from app.assistant.lib.core_tools.calendar_tool import calendar_tool as calendar_mod

    calls = []

    def _spy_edit_event(service, event_id, arguments, scope=None):
        calls.append({"service": service, "event_id": event_id, "arguments": dict(arguments), "scope": scope})
        # Return a minimal Google-shaped event so the handler's downstream code
        # doesn't bail. The field contents don't matter — _to_repo_calendar_event
        # is stubbed below.
        return {
            "id": event_id,
            "summary": "spy event",
            "start": {"dateTime": "2026-04-10T20:00:00-07:00"},
            "end": {"dateTime": "2026-04-10T22:00:00-07:00"},
        }

    monkeypatch.setattr(calendar_mod, "edit_event", _spy_edit_event)
    monkeypatch.setattr(calendar_mod, "normalize_google_event_times", lambda e: e)
    return calls


def _install_repo_mapper_stub(tool, monkeypatch):
    """_to_repo_calendar_event touches many fields; stub it to a no-op for these tests."""
    monkeypatch.setattr(
        tool,
        "_to_repo_calendar_event",
        lambda event: {"id": event.get("id", ""), "data_type": "calendar"},
        raising=False,
    )


# ----------------------------------------------------------------------
# Scope invariants
# ----------------------------------------------------------------------

def test_update_without_scope_forwards_scope_none_to_edit_event(monkeypatch):
    """
    If the planner omits scope, the handler must forward scope=None to
    edit_event — not silently default to 'all'. edit_event treats None as
    'whatever the underlying Google API defaults to for this event shape',
    which is the intended behavior.
    """
    tool, _fake = build_calendar_tool(monkeypatch)
    calls = _install_edit_event_spy(monkeypatch)
    _install_repo_mapper_stub(tool, monkeypatch)

    tool.handle_update_calendar_event({"event_id": "abc", "summary": "new title"})

    assert len(calls) == 1
    assert calls[0]["scope"] is None, (
        "Regression: handle_update_calendar_event silently defaulted scope — "
        "planner intent must be preserved."
    )


def test_update_with_scope_single_forwards_exactly_single(monkeypatch):
    tool, _fake = build_calendar_tool(monkeypatch)
    calls = _install_edit_event_spy(monkeypatch)
    _install_repo_mapper_stub(tool, monkeypatch)

    tool.handle_update_calendar_event({"event_id": "abc_20260411T030000Z", "summary": "new", "scope": "single"})

    assert len(calls) == 1
    assert calls[0]["scope"] == "single"


def test_update_with_scope_all_forwards_exactly_all(monkeypatch):
    tool, _fake = build_calendar_tool(monkeypatch)
    calls = _install_edit_event_spy(monkeypatch)
    _install_repo_mapper_stub(tool, monkeypatch)

    tool.handle_update_calendar_event({"event_id": "abc", "summary": "new", "scope": "all"})

    assert len(calls) == 1
    assert calls[0]["scope"] == "all"


def test_update_pops_scope_from_arguments_before_passing_to_edit_event(monkeypatch):
    """
    scope is a handler-level control, not a Google Calendar field. It must be
    extracted BEFORE arguments are forwarded to edit_event — otherwise Google
    would reject it, and it'd pollute the updated-event shape.
    """
    tool, _fake = build_calendar_tool(monkeypatch)
    calls = _install_edit_event_spy(monkeypatch)
    _install_repo_mapper_stub(tool, monkeypatch)

    tool.handle_update_calendar_event({"event_id": "abc", "summary": "s", "scope": "single"})

    assert "scope" not in calls[0]["arguments"], (
        "Regression: scope leaked into Google's update payload via edit_event arguments."
    )


# ----------------------------------------------------------------------
# Argument normalization
# ----------------------------------------------------------------------

def test_update_requires_event_id(monkeypatch):
    tool, _fake = build_calendar_tool(monkeypatch)
    _install_edit_event_spy(monkeypatch)
    _install_repo_mapper_stub(tool, monkeypatch)

    result = tool.handle_update_calendar_event({"summary": "no id"})
    assert result.result_type == "error"


def test_update_filters_out_none_and_empty_string_fields(monkeypatch):
    """
    Handler strips fields whose value is None or '' before hitting edit_event —
    prevents accidentally wiping descriptions/locations when the planner
    didn't mean to touch them.
    """
    tool, _fake = build_calendar_tool(monkeypatch)
    calls = _install_edit_event_spy(monkeypatch)
    _install_repo_mapper_stub(tool, monkeypatch)

    tool.handle_update_calendar_event({
        "event_id": "abc",
        "summary": "new title",
        "description": None,   # should be dropped
        "location": "",        # should be dropped
    })

    args = calls[0]["arguments"]
    assert "description" not in args
    assert "location" not in args
    assert args.get("summary") == "new title"


def test_update_wraps_string_recurrence_in_list(monkeypatch):
    """Google expects recurrence as a list; handler must normalize a bare string."""
    tool, _fake = build_calendar_tool(monkeypatch)
    calls = _install_edit_event_spy(monkeypatch)
    _install_repo_mapper_stub(tool, monkeypatch)

    tool.handle_update_calendar_event({
        "event_id": "abc",
        "recurrence": "RRULE:FREQ=WEEKLY;BYDAY=FR",
    })

    assert calls[0]["arguments"]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=FR"]
