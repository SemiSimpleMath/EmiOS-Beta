"""
Shared helpers for the tool test suite.

Keep this file tiny — it exists only to cut boilerplate from per-tool test files.
Anything that's specific to one tool's internals belongs in that tool's own test file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional  # noqa: F401

from app.assistant.utils.pydantic_classes import ToolMessage


def make_tool_message(tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolMessage:
    """Build a ToolMessage in the shape tool handlers expect."""
    return ToolMessage(
        tool_name=tool_name,
        tool_data={"tool_name": tool_name, "arguments": dict(arguments or {})},
    )


# ----------------------------------------------------------------------
# Fake Google Calendar service
# ----------------------------------------------------------------------
# The real Google Calendar service is accessed as:
#     service.events().get(calendarId=..., eventId=...).execute()
#     service.events().delete(calendarId=..., eventId=...).execute()
#
# FakeCalendarService provides that fluent shape while recording calls so
# tests can assert exactly which eventId was passed to which operation.

@dataclass
class _CalledOp:
    op: str  # "get" or "delete"
    calendar_id: str
    event_id: str


@dataclass
class _FakeRequest:
    _op: str
    _calendar_id: str
    _event_id: str
    _calls_out: List[_CalledOp]
    _get_responses: Dict[str, Dict[str, Any]]

    def execute(self):
        self._calls_out.append(_CalledOp(op=self._op, calendar_id=self._calendar_id, event_id=self._event_id))
        if self._op == "get":
            if self._event_id not in self._get_responses:
                raise KeyError(
                    f"FakeCalendarService.events().get() called with unregistered event_id={self._event_id!r}. "
                    f"Register via set_event()."
                )
            return self._get_responses[self._event_id]
        if self._op == "delete":
            return None
        raise ValueError(f"Unknown op {self._op!r}")


@dataclass
class _FakeEvents:
    _calls: List[_CalledOp]
    _get_responses: Dict[str, Dict[str, Any]]

    def get(self, *, calendarId: str, eventId: str) -> _FakeRequest:
        return _FakeRequest("get", calendarId, eventId, self._calls, self._get_responses)

    def delete(self, *, calendarId: str, eventId: str) -> _FakeRequest:
        return _FakeRequest("delete", calendarId, eventId, self._calls, self._get_responses)


@dataclass
class _FakeCalendarListRequest:
    """Stub for service.calendarList().list(pageToken=...).execute() — returns
    a single page with one default calendar so get_events can resolve calendar IDs."""
    _calendars: List[Dict[str, Any]]

    def execute(self) -> Dict[str, Any]:
        return {"items": list(self._calendars)}


@dataclass
class _FakeCalendarList:
    _calendars: List[Dict[str, Any]]

    def list(self, *, pageToken: Optional[str] = None) -> _FakeCalendarListRequest:  # noqa: N803
        return _FakeCalendarListRequest(self._calendars)


@dataclass
class FakeCalendarService:
    """
    Minimal fake of the Google Calendar service surface used by delete_event,
    delete_recurring_event, and get_calendar_events (which enumerates
    calendarList() before fetching events).

    Usage:
        svc = FakeCalendarService()
        svc.set_event("abc", {"id": "abc"})                    # non-recurring
        svc.set_event("abc_20260411T030000Z", {                # instance of recurring
            "id": "abc_20260411T030000Z",
            "recurringEventId": "abc",
        })
        svc.set_event("xyz", {"id": "xyz", "recurrence": ["RRULE:FREQ=WEEKLY"]})  # base series
    """
    calls: List[_CalledOp] = field(default_factory=list)
    _get_responses: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Default to one calendar named "primary" so calendarList().list().execute()
    # returns something useful for tests that don't override.
    _calendars: List[Dict[str, Any]] = field(
        default_factory=lambda: [{"id": "primary", "summary": "primary"}]
    )

    def set_event(self, event_id: str, event: Dict[str, Any]) -> None:
        self._get_responses[event_id] = event

    def events(self) -> _FakeEvents:
        return _FakeEvents(self.calls, self._get_responses)

    def calendarList(self) -> _FakeCalendarList:  # noqa: N802 — matches Google API name
        return _FakeCalendarList(self._calendars)

    # Convenience query helpers for tests --------------------------------

    def get_calls_of(self, op: str) -> List[_CalledOp]:
        return [c for c in self.calls if c.op == op]

    def last_delete_target(self) -> Optional[str]:
        deletes = self.get_calls_of("delete")
        return deletes[-1].event_id if deletes else None


# ----------------------------------------------------------------------
# Stand-in collaborators for CalendarTool side effects
# ----------------------------------------------------------------------
# CalendarTool handlers touch a few shared collaborators (event repo, event
# hub, event-node manager). We don't test those here — we just need them to
# not explode during handler execution.

class _NoopRepoManager:
    def delete_event(self, *_, **__): pass
    def store_event(self, *_, **__): pass


class _NoopEventHub:
    def publish(self, *_, **__): pass
    def register_event(self, *_, **__): pass


def install_calendar_tool_test_doubles(tool, monkeypatch) -> FakeCalendarService:
    """
    Wire a freshly-constructed CalendarTool up with test doubles so its
    delete handlers can run without hitting Google, SQLite, or the event graph.
    Returns the FakeCalendarService so tests can register events + assert calls.
    """
    fake = FakeCalendarService()
    tool.service = fake
    tool.repo_manager = _NoopRepoManager()
    monkeypatch.setattr(tool, "_cleanup_event_node", lambda *a, **k: None, raising=False)
    # DI.event_hub is read inside the handler; stub it out.
    from app.assistant.lib.core_tools.calendar_tool import calendar_tool as calendar_tool_mod
    monkeypatch.setattr(calendar_tool_mod.DI, "event_hub", _NoopEventHub(), raising=False)
    return fake


def build_calendar_tool(monkeypatch):
    """
    Build a CalendarTool safe for unit tests: no real auth, no real DB, no real
    event hub. Returns (tool, fake_service).
    """
    from app.assistant.lib.core_tools.calendar_tool.calendar_tool import CalendarTool
    tool = CalendarTool.__new__(CalendarTool)  # bypass __init__ (avoids EventRepositoryManager side effects)
    tool.name = "calendar_tool"
    fake_service = install_calendar_tool_test_doubles(tool, monkeypatch)
    return tool, fake_service
