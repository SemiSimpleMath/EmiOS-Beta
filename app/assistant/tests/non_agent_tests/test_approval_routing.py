"""Wall #3 — tool-approval routes to the OWNER, never the requester's surface.

The breach raised a send_email approval and routed the /approve prompt to the
GUEST's own telegram chat (which failed "chat not found"). A non-owner room must
never be asked to approve an action on the owner's resources. ApprovalGateway.request
now ALWAYS raises an owner ticket (homed to master_room, globally visible in the
owner's UI), with a provenance line so the owner can tell a guest request from their
own — regardless of the requesting surface (telegram/slack/sms/internal).

Run:
  .venv\\Scripts\\python.exe -m pytest app/assistant/tests/non_agent_tests/test_approval_routing.py
"""
from __future__ import annotations

from types import SimpleNamespace

import app.assistant.lib.core_tools.approval_gateway.approval_gateway as ag
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeToolPolicy,
)


class _FakeTicketManager:
    """Captures create_ticket and reports the ticket as immediately accepted, so
    the gateway's poll loop returns without a real wait."""

    def __init__(self):
        self.created: dict | None = None

    def create_ticket(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(ticket_id="tk_test")

    def mark_proposed(self, ticket_id):
        return True

    def get_ticket_by_id(self, ticket_id):
        return SimpleNamespace(ticket_id=ticket_id, state="accepted")

    def mark_executing(self, ticket_id):
        return True


def _scope(surface: str, room_id: str, actor: str, authority: int) -> ScopeContext:
    return ScopeContext(
        scope_id="s", owner_id="u", actor_id=actor, surface=surface, room_id=room_id,
        approval=ScopeApprovalPolicy(authority_level=authority),
        tools=ScopeToolPolicy(allowed_tools=["all"]),
    )


def _request(monkeypatch, scope, blackboard=None):
    tm = _FakeTicketManager()
    monkeypatch.setattr(ag, "DI", SimpleNamespace(ticket_manager=tm))
    approved, ticket_id, blocked = ag.request(
        tool_name="send_email",
        title="Allow send_email?",
        message="to: owner@example.com",
        calling_agent="personal_admin::planner",
        approval_reasons=["tool_contract.metadata.approval_min_authority"],
        scope_context=scope,
        blackboard=blackboard,
        timeout_seconds=5,
    )
    return tm, approved, ticket_id, blocked


def test_guest_telegram_approval_goes_to_owner_not_guest_chat(monkeypatch):
    # The breach scenario: a telegram guest triggers send_email approval.
    tm, approved, tid, _ = _request(
        monkeypatch, _scope("telegram", "telegram/guest_chat", "U_guest", 40))
    assert approved is True and tid == "tk_test"
    tc = tm.created["trigger_context"]
    # Homed to the OWNER's room, NOT the guest's telegram room.
    assert tc["room_id"] == "master_room"
    assert tc["origin_room_id"] == "telegram/guest_chat"
    # Provenance the owner sees so a guest request isn't blind-approved.
    msg = tm.created["message"]
    assert "telegram/guest_chat" in msg
    assert "authority 40" in msg
    assert "U_guest" in msg  # no blackboard contact name -> falls back to actor_id


def test_slack_approval_also_uses_ticket_not_inline(monkeypatch):
    # Slack is an inline surface; it must still go to the owner ticket (inline retired).
    tm, approved, _, _ = _request(
        monkeypatch, _scope("slack", "slack/guest_channel", "U_guest2", 30))
    assert approved is True
    assert tm.created["trigger_context"]["room_id"] == "master_room"
    assert tm.created["action_params"]["origin_room_id"] == "slack/guest_channel"


def test_contact_name_preferred_in_provenance(monkeypatch):
    # When the room has a human contact name, the provenance uses it.
    bb = SimpleNamespace(get_state_value=lambda k, *a: "Guest User" if k == "room_contact_name" else None)
    tm, approved, _, _ = _request(
        monkeypatch, _scope("telegram", "telegram/guest_chat", "U_guest", 40), blackboard=bb)
    assert "Guest User" in tm.created["message"]


def test_no_origin_room_still_routes_to_owner(monkeypatch):
    # A system/internal request with no room still reaches the owner (no raise).
    tm, approved, _, _ = _request(
        monkeypatch, _scope("internal", "", "system", 95))
    assert approved is True
    assert tm.created["trigger_context"]["room_id"] == "master_room"
    assert "an unknown room" in tm.created["message"]


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
