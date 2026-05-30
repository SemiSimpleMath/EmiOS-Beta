"""Parity: the two room-scope ingress paths produce identical permission.

The unified-scope refactor collapsed scope_adapter._derive_room_scope (the
system/internal -> manager-with-room_id path) onto the single reference builder
room_scope_builder.build_scope_contract_for_room_request (the chat-ingress path).

Before the collapse, the SAME room reached two ways yielded two different scopes:
chat ingress had tools.per_manager + pods + skills + the scope.yaml overlay;
the manager-ingress derivation had none of them (broader/weaker). This test
guards that they now agree on every permission block, so a room's declared
posture holds regardless of entry vector.

Uses slack/__test__ (a committed, deterministic room WITH a scope.yaml whose
per_manager rules are exactly what would have diverged).

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_room_scope_ingress_parity.py
"""
from __future__ import annotations

from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
from app.assistant.room_session_manager.services.room_scope_builder import (
    build_scope_contract_for_room_request,
)
from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.utils.pydantic_classes import Message, ScopeContext

_ROOM_ID = "slack/__test__"
# Permission blocks that must match across ingress paths (identity / scope_id /
# history timestamps are request-shaped and excluded).
_PERMISSION_BLOCKS = ("tools", "pods", "resources", "entities", "cards", "writes", "approval")


def _chat_ingress_scope() -> ScopeContext:
    room_ctx = load_room_context_for_manager(_ROOM_ID)
    env = InboundEnvelope(
        surface="slack", room_id=_ROOM_ID, context_id="main", request_id="r1",
        speaker_name="TestFriend", speaker_id="U_test", speaker_external_id="U_test",
        content="hi", timestamp_local="", inbound_line="", transport_message_id="",
        transport_from="U_test", transport_to=_ROOM_ID,
    )
    d = build_scope_contract_for_room_request(
        room_ctx=room_ctx, envelope=env,
        request_data={"task_allowed_tools": None, "task_except_tools": None,
                      "reply_to": None, "actas_principal": "user"},
    )
    return ScopeContext.model_validate(d)


def _manager_ingress_scope() -> ScopeContext:
    # A system/internal Message arriving at a manager with room_id but no scope.
    msg = Message(
        room_id=_ROOM_ID, room_surface="slack", room_context_id="main",
        room_speaker_external_id="U_test", content="hi",
    )
    return ScopeAdapter()._derive_room_scope(message=msg)


def test_both_ingress_paths_yield_same_permission_blocks():
    chat = _chat_ingress_scope().model_dump()
    mgr = _manager_ingress_scope().model_dump()
    for block in _PERMISSION_BLOCKS:
        assert mgr[block] == chat[block], f"ingress divergence in permission block '{block}'"


def test_manager_ingress_now_has_per_manager():
    # The headline regression that the collapse fixes: per_manager was missing.
    mgr = _manager_ingress_scope()
    assert "emi_team_manager" in mgr.tools.per_manager
    assert mgr.tools.per_manager["web_manager"].block == ["http_request", "oauth_token_refresh"]


def test_manager_ingress_now_has_pods_block():
    mgr = _manager_ingress_scope()
    assert mgr.pods.allowed_scopes == ["self"]


def test_manager_ingress_authority_matches_room():
    # slack/__test__ is authority 30; the old derivation read it from policy too,
    # but assert it explicitly so a drift in the bridge is caught.
    assert _manager_ingress_scope().approval.authority_level == 30


def test_identity_still_bridged_from_message():
    mgr = _manager_ingress_scope()
    assert mgr.room_id == _ROOM_ID
    assert mgr.surface == "slack"
    assert mgr.actor_id == "U_test"


def test_no_room_id_returns_none():
    assert ScopeAdapter()._derive_room_scope(message=Message(content="x")) is None
