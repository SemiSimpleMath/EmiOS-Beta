"""Step 2b: scope.yaml overlay in the room scope builder.

Proves:
- _overlay_scope_yaml_permission makes a room's scope.yaml AUTHORITATIVE for the
  permission bucket (tools/per_manager, pods, approval, resources, entities,
  cards, writes, delivery permission bits) while PRESERVING builder-computed
  identity + room-behavior (history/retention/execution) + derived
  (delivery.allowed_reply_types).
- It is a no-op for a room WITHOUT a scope.yaml.
- End-to-end through build_scope_contract_for_room_request: slack/__test__ comes
  out with scope.yaml permission AND builder room-behavior intact (no regression).

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_room_scope_yaml_overlay.py
"""
from __future__ import annotations

from app.assistant.room_session_manager.services.room_scope_builder import (
    _overlay_scope_yaml_permission,
    build_scope_contract_for_room_request,
)


_ROOM_ID = "slack/__test__"


def _identity_block() -> dict:
    return {
        "scope_id": "scope::slack::slack/__test__::main::deadbeef",
        "owner_id": _ROOM_ID,
        "actor_id": "U_test",
        "surface": "slack",
        "room_id": _ROOM_ID,
        "room_context_id": "main",
        "visibility": "room_shared",
        "policy_id": "room_policy::slack/__test__::v1",
        "reply_to": None,
        "acting_as": "user",
    }


# ---------------------------------------------------------------------------
# overlay mechanics
# ---------------------------------------------------------------------------

def test_overlay_makes_scope_yaml_authoritative_for_permission():
    # A base dict that DELIBERATELY disagrees with scope.yaml on every
    # permission axis — proving the file wins, not coincidental equality.
    base = {
        **_identity_block(),
        "schema_version": "scope_context_v1",
        # behavior we expect PRESERVED:
        "history": {"mode": "summary_plus_recent", "source": "unified_log",
                    "include_room_scoped": True, "lookback_hours": 48,
                    "max_messages": None, "max_chars_per_message": None},
        "retention": {"persist_chat": True, "persist_tool_results": True,
                      "allow_context_summarization": True, "redact_before_persist": False},
        "execution": {"max_turns": 7, "max_tool_calls": None, "timeout_seconds": None, "allowed_models": []},
        "skills": {"always_inject": ["some_pack"], "denied_skills": []},
        # permission we expect OVERWRITTEN by scope.yaml:
        "approval": {"authority_level": 99},
        "tools": {"allowed_tools": ["all"], "blocked_tools": [],
                  "requires_approval_tools": [], "allow_external_side_effects": True,
                  "per_manager": {}},
        "pods": {"allowed_scopes": ["all"]},
        "resources": {"allowed_global_resources": ["all"], "allowed_room_resources": [],
                      "denied_resources": [], "resource_groups": []},
        "entities": {"enabled": True, "allowed_entity_cards": [], "pinned_entities": [],
                     "entity_lookback_messages": None, "entity_lookback_seconds": None},
        "cards": {"enabled": True, "allowed_cards": [], "max_cards_per_turn": None, "max_total_chars": None},
        "writes": {"write_unified_log": True, "write_kg": True, "allow_fact_extraction": True,
                   "writable_state_keys": []},
        # delivery: permission bits should flip to file; allowed_reply_types PRESERVED.
        "delivery": {"auto_send": False, "allow_initiation": False, "allowed_reply_types": ["slack"]},
        "delegation": {},
    }

    out = _overlay_scope_yaml_permission(room_id=_ROOM_ID, scope_dict=base)

    # --- file wins on permission ---
    assert out["approval"]["authority_level"] == 30
    assert "emi_team_manager" in out["tools"]["per_manager"]
    assert out["tools"]["per_manager"]["web_manager"]["block"] == ["http_request", "oauth_token_refresh"]
    assert out["pods"]["allowed_scopes"] == ["self"]
    assert out["writes"]["write_kg"] is False           # file says false, base said true
    assert out["tools"]["allow_external_side_effects"] is False
    assert out["resources"]["resource_groups"] == ["chat", "memory"]

    # --- delivery: permission bits from file, derived preserved ---
    assert out["delivery"]["auto_send"] is True          # file
    assert out["delivery"]["allow_initiation"] is True   # file
    assert out["delivery"]["allowed_reply_types"] == ["slack"]  # builder-derived, preserved

    # --- behavior + identity preserved from base ---
    assert out["history"]["mode"] == "summary_plus_recent"
    assert out["execution"]["max_turns"] == 7
    assert out["skills"]["always_inject"] == ["some_pack"]
    assert out["actor_id"] == "U_test"
    assert out["scope_id"] == base["scope_id"]


def test_overlay_is_noop_without_scope_yaml():
    # A room with no scope.yaml on disk — overlay returns the dict unchanged.
    # Uses a synthetic room_id so this stays a true no-op regardless of which
    # real rooms get migrated later.
    rid = "noop_probe_room_no_scope_yaml"
    base = {**_identity_block(), "owner_id": rid, "room_id": rid,
            "approval": {"authority_level": 99}}
    out = _overlay_scope_yaml_permission(room_id=rid, scope_dict=base)
    assert out == base
    assert out["approval"]["authority_level"] == 99  # untouched


def test_overlay_is_noop_with_blank_room_id():
    base = {**_identity_block(), "approval": {"authority_level": 55}}
    out = _overlay_scope_yaml_permission(room_id="", scope_dict=base)
    assert out == base


# ---------------------------------------------------------------------------
# end-to-end through the real builder
# ---------------------------------------------------------------------------

def _build_full() -> dict:
    from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
    from app.assistant.tests.room_test_harness.harness import _build_envelope

    room_ctx = load_room_context_for_manager(_ROOM_ID)
    env = _build_envelope(
        surface="slack", room_id=_ROOM_ID, content="hi",
        speaker_name="TestFriend", speaker_external_id="U_test", context_id="main",
    )
    return build_scope_contract_for_room_request(
        room_ctx=room_ctx,
        envelope=env,
        request_data={"task_allowed_tools": None, "task_except_tools": None,
                      "reply_to": None, "actas_principal": "user"},
    )


def test_builder_permission_comes_from_scope_yaml():
    d = _build_full()
    assert d["approval"]["authority_level"] == 30
    assert d["tools"]["per_manager"]["emi_team_manager"]["allow"][:3] == [
        "web_manager", "pod_search", "pod_fetch",
    ]
    assert d["tools"]["per_manager"]["web_manager"]["block"] == [
        "http_request", "oauth_token_refresh",
    ]
    assert d["pods"]["allowed_scopes"] == ["self"]


def test_builder_room_behavior_preserved():
    d = _build_full()
    # room-behavior + derived survive the overlay (NOT in scope.yaml)
    assert d["history"]["mode"] == "summary_plus_recent"
    assert d["history"]["lookback_hours"] == 48
    assert d["retention"]["persist_tool_results"] is True
    assert d["delivery"]["allowed_reply_types"] == ["slack"]
    # identity stamped
    assert d["owner_id"] == _ROOM_ID
    assert d["actor_id"] == "U_test"
