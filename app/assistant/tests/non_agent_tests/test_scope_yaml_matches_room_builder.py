"""Characterization test: slack/__test__/scope.yaml reproduces the PERMISSION
fields the legacy room builder emits for that room.

This is the Step-2a proof-of-equivalence. It touches NO production code — it
builds the golden scope_dict via the existing
``build_scope_contract_for_room_request`` and asserts that
``load_scope(scope.yaml, identity)`` produces the same PERMISSION bucket.

Permission-only by design (see project_unified_scope_design "three buckets"):
- ASSERTED: tools (allowed_tools + per_manager), pods, approval, resources,
  entities, writes, delivery.{auto_send, allow_initiation}.
- NOT asserted: history / retention / execution (room behavior, stay in
  ROOM.md), delivery.allowed_reply_types + skills (stamped from request),
  scope_id (random).

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_scope_yaml_matches_room_builder.py
"""
from __future__ import annotations

import pytest

from app.assistant.rooms.room_resource_loader import resolve_room_config_dir
from app.assistant.scope.loader import load_scope
from app.assistant.utils.pydantic_classes import ScopeContext


_ROOM_ID = "slack/__test__"
# Canonical room-dir resolver — never hand-roll Path(__file__).parents[N].
_SCOPE_YAML = resolve_room_config_dir(_ROOM_ID) / "scope.yaml"

# Identity envelope matching the golden builder run (see harness _build_envelope:
# actor_id = speaker_external_id, owner_id = room_id).
_IDENTITY = {
    "owner_id": _ROOM_ID,
    "actor_id": "U_test",
    "surface": "slack",
    "room_id": _ROOM_ID,
    "room_context_id": "main",
    "visibility": "room_shared",
    "policy_id": "room_policy::slack/__test__::v1",
    "acting_as": "user",
    "reply_to": None,
}


def _golden_scope() -> ScopeContext:
    """The scope the CURRENT production builder emits for slack/__test__."""
    from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
    from app.assistant.room_session_manager.services.room_scope_builder import (
        build_scope_contract_for_room_request,
    )
    from app.assistant.tests.e2e.harness import _build_envelope

    room_ctx = load_room_context_for_manager(_ROOM_ID)
    env = _build_envelope(
        surface="slack",
        room_id=_ROOM_ID,
        content="hi",
        speaker_name="TestFriend",
        speaker_external_id="U_test",
        context_id="main",
    )
    d = build_scope_contract_for_room_request(
        room_ctx=room_ctx,
        envelope=env,
        request_data={
            "task_allowed_tools": None,
            "task_except_tools": None,
            "reply_to": None,
            "actas_principal": "user",
        },
    )
    return ScopeContext.model_validate(d)


@pytest.fixture(scope="module")
def golden() -> ScopeContext:
    return _golden_scope()


@pytest.fixture(scope="module")
def loaded() -> ScopeContext:
    assert _SCOPE_YAML.exists(), f"missing {_SCOPE_YAML}"
    return load_scope(_SCOPE_YAML, identity=_IDENTITY)


# ---------------------------------------------------------------------------
# permission-bucket equivalence
# ---------------------------------------------------------------------------

def test_tools_allowed_tools(golden, loaded):
    assert loaded.tools.allowed_tools == golden.tools.allowed_tools


def test_tools_blocked_tools(golden, loaded):
    assert loaded.tools.blocked_tools == golden.tools.blocked_tools


def test_per_manager_rules_match(golden, loaded):
    def norm(pm):
        return {
            name: (rule.allow, rule.block)
            for name, rule in pm.items()
        }
    assert norm(loaded.tools.per_manager) == norm(golden.tools.per_manager)


def test_pods(golden, loaded):
    assert loaded.pods.allowed_scopes == golden.pods.allowed_scopes


def test_approval_authority(golden, loaded):
    assert loaded.approval.authority_level == golden.approval.authority_level == 30


def test_resources(golden, loaded):
    assert (
        loaded.resources.allowed_global_resources
        == golden.resources.allowed_global_resources
    )
    assert loaded.resources.resource_groups == golden.resources.resource_groups


def test_entities(golden, loaded):
    assert loaded.entities.enabled == golden.entities.enabled
    assert loaded.entities.allowed_entity_cards == golden.entities.allowed_entity_cards


def test_writes(golden, loaded):
    assert loaded.writes.write_unified_log == golden.writes.write_unified_log
    assert loaded.writes.write_kg == golden.writes.write_kg
    assert loaded.writes.allow_fact_extraction == golden.writes.allow_fact_extraction


def test_delivery_permission_bits(golden, loaded):
    # auto_send / allow_initiation are permission; allowed_reply_types is stamped.
    assert loaded.delivery.auto_send == golden.delivery.auto_send
    assert loaded.delivery.allow_initiation == golden.delivery.allow_initiation


# ---------------------------------------------------------------------------
# identity stamping sanity
# ---------------------------------------------------------------------------

def test_identity_stamped(loaded):
    assert loaded.owner_id == _ROOM_ID
    assert loaded.actor_id == "U_test"
    assert loaded.surface == "slack"
    assert loaded.visibility == "room_shared"
    assert loaded.acting_as == "user"
