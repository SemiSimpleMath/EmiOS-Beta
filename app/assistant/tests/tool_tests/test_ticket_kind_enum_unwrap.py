"""The composer's ticket_kind reaches the tool as a value, not an enum repr.

`_format_brief` reads the composer's structured output off the manager
blackboard. `ticket_kind` there is a `TicketKind` member, and `TicketKind`
subclasses `(str, Enum)` — so `str()` on it returns "TicketKind.notify"
(Enum.__str__) rather than "notify". Lowercased downstream that becomes
'ticketkind.notify', which `_ui_policy_for_ticket_kind` refuses.

The failure only appears when the composer SUCCEEDS: the exception path returns
plain strings and the `or "advice"` default returns a plain string, so both of
those work. That is why it ran 66 times in one session before anyone saw it —
every iteration paid for a full ticket_builder_manager run, got a correct
answer, and threw it away on the last hop.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/tool_tests/test_ticket_kind_enum_unwrap.py
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

import pytest

from app.assistant.agents.ticket_builder.composer.agent_form import TicketKind
from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
    CreateDayflowTicketTool,
)


def test_str_on_the_enum_is_the_trap_this_guards():
    """Pin the language behaviour the bug rested on, so it stays visible."""
    assert str(TicketKind.notify) != "notify"
    assert str(TicketKind.notify).lower() == "ticketkind.notify"
    assert TicketKind.notify.value == "notify"


@pytest.mark.parametrize("kind", list(TicketKind))
def test_every_ticket_kind_survives_the_round_trip(kind):
    """An enum member from the composer must come out as its bare value."""
    unwrapped = getattr(kind, "value", kind)
    assert str(unwrapped) == kind.value
    # The value must be one the UI policy accepts, which is the check that
    # was raising in production.
    layout, _plan_mode = CreateDayflowTicketTool._ui_policy_for_ticket_kind(
        str(unwrapped).lower()
    )
    assert layout == kind.value


def test_ui_policy_still_refuses_a_stringified_enum():
    """The guard that caught this must keep catching it."""
    with pytest.raises(ValueError, match="ticket_kind must be one of"):
        CreateDayflowTicketTool._ui_policy_for_ticket_kind("ticketkind.notify")


def test_ui_policy_accepts_the_three_real_kinds():
    assert CreateDayflowTicketTool._ui_policy_for_ticket_kind("notify") == ("notify", False)
    assert CreateDayflowTicketTool._ui_policy_for_ticket_kind("decision") == ("decision", True)
    assert CreateDayflowTicketTool._ui_policy_for_ticket_kind("advice") == ("advice", True)
