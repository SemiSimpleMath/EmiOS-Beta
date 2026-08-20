"""ask_user no-answer semantics: the user's absence is not a tool failure (2026-08-20).

An unanswered question used to return abort_policy="abort_task", so the
fail-closed policy killed the whole calling manager and the narrator invented a
tool-failure story ("I can't check the calendar - tool failed"). Now every
timeout path returns a SURVIVABLE abort_tool error whose text instructs the
calling agent directly: proceed on best judgment, or finish and state what is
still needed. abort_task remains only for real infrastructure failures
(missing ticket manager, lost ticket, bad input).
"""
from __future__ import annotations

from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.lib.tools.ask_user import ask_user as ask_user_mod
from app.assistant.lib.tools.ask_user.ask_user import AskUserTool, _UNREACHABLE_MESSAGE
from app.assistant.utils.pydantic_classes import ToolMessage


class _StubTicketManager:
    """Serves a scripted sequence of ticket states, one per poll."""

    def __init__(self, states):
        self._states = list(states)
        self.expired = []
        self.user_text = None

    def create_ticket(self, **kwargs):
        return SimpleNamespace(ticket_id="tk_test")

    def mark_proposed(self, ticket_id):
        return True

    def get_ticket_by_id(self, ticket_id):
        state = self._states.pop(0) if self._states else "proposed"
        return SimpleNamespace(ticket_id=ticket_id, state=state, user_text=self.user_text)

    def mark_expired(self, ticket_id):
        self.expired.append(ticket_id)


def _run(monkeypatch, *, states, timeout_seconds, user_text=None):
    stub = _StubTicketManager(states)
    stub.user_text = user_text
    monkeypatch.setattr(ask_user_mod.DI, "ticket_manager", stub, raising=False)
    monkeypatch.setattr(ask_user_mod.time, "sleep", lambda _s: None)
    tool = AskUserTool()
    msg = ToolMessage(
        tool_name="ask_user",
        tool_data={
            "tool_name": "ask_user",
            "arguments": {"text": "Were Bonnie and Clyde walked this morning?"},
            "request_context": {"ask_user_timeout_seconds": timeout_seconds},
        },
    )
    return tool.execute(msg), stub


def test_timeout_is_survivable_and_instructs_the_agent(monkeypatch):
    result, stub = _run(monkeypatch, states=["proposed", "proposed"], timeout_seconds=2)
    assert result.result_type == "error"
    assert result.data["abort_policy"] == "abort_tool"
    assert result.data["error_code"] == "ask_user_timeout"
    assert result.content == _UNREACHABLE_MESSAGE
    assert stub.expired == ["tk_test"]


def test_externally_expired_ticket_is_survivable(monkeypatch):
    result, _ = _run(monkeypatch, states=["expired"], timeout_seconds=300)
    assert result.result_type == "error"
    assert result.data["abort_policy"] == "abort_tool"
    assert result.data["error_code"] == "ask_user_timeout"
    assert result.content == _UNREACHABLE_MESSAGE


def test_dismissal_stays_survivable_and_distinct(monkeypatch):
    result, _ = _run(monkeypatch, states=["dismissed"], timeout_seconds=300)
    assert result.result_type == "error"
    assert result.data["abort_policy"] == "abort_tool"
    assert result.data["error_code"] == "ask_user_dismissed"
    assert result.content != _UNREACHABLE_MESSAGE  # declined != unreachable


def test_answer_still_flows_through(monkeypatch):
    result, _ = _run(
        monkeypatch,
        states=["accepted"],
        timeout_seconds=300,
        user_text="Yes, both walked and fed.",
    )
    assert result.result_type == "ask_user_response"
    assert result.content == "Yes, both walked and fed."


def test_infrastructure_failure_still_aborts_the_task(monkeypatch):
    monkeypatch.setattr(ask_user_mod.DI, "ticket_manager", None, raising=False)
    tool = AskUserTool()
    msg = ToolMessage(
        tool_name="ask_user",
        tool_data={"tool_name": "ask_user", "arguments": {"text": "Q?"}},
    )
    result = tool.execute(msg)
    assert result.result_type == "error"
    assert result.data["abort_policy"] == "abort_task"
