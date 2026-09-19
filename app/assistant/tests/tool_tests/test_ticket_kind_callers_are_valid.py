"""Every programmatic caller of create_dayflow_ticket passes a kind the tool accepts.

`_ui_policy_for_ticket_kind` accepts exactly notify / decision / advice and RAISES on
anything else — and it runs BEFORE the ticket is created. So an invalid `ticket_kind` is
not a cosmetic problem: no ticket exists, and `execute`'s outer except turns the raise
into an error ToolResult.

Two callers shipped `ticket_kind: "info"`, which is not a kind:

  * ring_analysis/camera_dispatcher._fire_dayflow_ticket — the high-stakes door event
    escalation.
  * subconscious/scheduler_arbiter_persist — the "the arbiter punted, ask the user"
    conflict ticket.

Both therefore failed every time they ran. Neither had run in any retained log, so this
was latent rather than observed: it would have failed the first time a significant door
event or a scheduling conflict occurred. Fixed 2026-09-18 to notify / decision.

The check is an AST walk rather than a string search, so it covers any future call site
in those modules without depending on how the dict literal is written.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/tool_tests/test_ticket_kind_callers_are_valid.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import app.assistant.tests.test_setup  # noqa: F401

import pytest

from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
    CreateDayflowTicketTool,
)
from app.assistant.utils.pydantic_classes import ToolResult

_REPO_ROOT = Path(__file__).resolve().parents[4]

# Modules that build a create_dayflow_ticket argument dict themselves, rather than going
# through the switchboard's arguments node.
_PROGRAMMATIC_CALLERS = [
    "app/assistant/ring_analysis/camera_dispatcher.py",
    "app/assistant/subconscious/scheduler_arbiter_persist.py",
]


def _ticket_kinds_in(rel_path: str) -> list[str]:
    """Every constant string assigned to a "ticket_kind" dict key in one module."""
    source = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "ticket_kind"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found.append(value.value)
    return found


@pytest.mark.parametrize("rel_path", _PROGRAMMATIC_CALLERS)
def test_caller_declares_a_ticket_kind_at_all(rel_path):
    """A silent absence would pass the check below by having nothing to check."""
    assert _ticket_kinds_in(rel_path), (
        f"{rel_path} declares no literal ticket_kind — either the call moved or this "
        f"test is now looking in the wrong place. An empty result must not read as a pass."
    )


@pytest.mark.parametrize("rel_path", _PROGRAMMATIC_CALLERS)
def test_every_declared_ticket_kind_is_accepted(rel_path):
    for kind in _ticket_kinds_in(rel_path):
        # Raises ValueError if the kind is not notify/decision/advice.
        CreateDayflowTicketTool._ui_policy_for_ticket_kind(kind)


def test_info_is_still_refused_so_the_guard_has_teeth():
    """The exact value that shipped. If this ever stops raising, the guard went soft."""
    with pytest.raises(ValueError, match="ticket_kind must be one of"):
        CreateDayflowTicketTool._ui_policy_for_ticket_kind("info")


def test_camera_escalation_reports_failure_instead_of_swallowing_it():
    """An error ToolResult must read as failure.

    The original success test was `result_type in {"create_dayflow_ticket", "tool_result"}
    or (isinstance(data, dict) and not data.get("error_code"))`. It was wrong twice:
    neither result_type it names is what the tool returns on success ("ticket_response"),
    and an error result carries `data={}`, an empty dict whose missing error_code made the
    second clause TRUE. So a refused escalation returned True.
    """
    from app.assistant.ring_analysis import camera_dispatcher

    calls: list[str] = []

    class _RefusingTool:
        def execute(self, _tool_message):
            calls.append("execute")
            return ToolResult(
                result_type="error",
                content="create_dayflow_ticket failed: ticket_kind must be one of: "
                        "notify, decision, advice. Got: 'info'",
                data={},
            )

    class _Registry:
        @staticmethod
        def get_tool_class(_name):
            return _RefusingTool

    from app.assistant.ServiceLocator.service_locator import DI

    original = DI.tool_registry
    try:
        DI.tool_registry = _Registry()
        ok = camera_dispatcher._fire_dayflow_ticket(
            camera={"id": "front_door", "name": "Front Door"},
            data={"category": "person", "caption": "someone at the door"},
            target_jpeg=Path("unused.jpg"),
            captured_at_utc="2026-09-18T12:00:00+00:00",
            pod_id=None,
        )
    finally:
        DI.tool_registry = original

    assert calls == ["execute"], "the tool was never called"
    assert ok is False, "a refused ticket must not report success"


def test_camera_escalation_reports_success_on_a_real_ticket_response():
    """The shape the tool actually returns when a ticket was created and answered."""
    from app.assistant.ring_analysis import camera_dispatcher

    class _RespondingTool:
        def execute(self, _tool_message):
            return ToolResult(
                result_type="ticket_response",
                content="User responded: seen",
                data={"ticket_id": "dayflow_orchestrator_1", "action": "done"},
            )

    class _Registry:
        @staticmethod
        def get_tool_class(_name):
            return _RespondingTool

    from app.assistant.ServiceLocator.service_locator import DI

    original = DI.tool_registry
    try:
        DI.tool_registry = _Registry()
        ok = camera_dispatcher._fire_dayflow_ticket(
            camera={"id": "front_door", "name": "Front Door"},
            data={"category": "person", "caption": "someone at the door"},
            target_jpeg=Path("unused.jpg"),
            captured_at_utc="2026-09-18T12:00:00+00:00",
            pod_id=None,
        )
    finally:
        DI.tool_registry = original

    assert ok is True
