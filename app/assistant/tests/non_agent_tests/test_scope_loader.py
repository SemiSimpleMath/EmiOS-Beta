"""Unit tests for the unified scope loader (app/assistant/scope/loader.py).

Covers the Step-1 contract:
- a valid declaration round-trips into a ScopeContext
- per_manager / pods declarations survive the load
- fail-closed floor: omitting tools.allowed_tools yields [] (not ["all"])
- an explicit allow-list (and the ["all"] marker) open the surface
- identity is stamped from the envelope; identity keys in the file are ignored
- missing required identity (owner_id/actor_id/surface) raises
- unknown keys are rejected (extra="forbid")
- a missing file raises FileNotFoundError; a non-mapping file raises ValueError

Run:
    .venv\\Scripts\\python.exe -m pytest app/assistant/tests/non_agent_tests/test_scope_loader.py
"""
from __future__ import annotations

import textwrap

import pytest

from app.assistant.scope.loader import load_scope
from app.assistant.utils.pydantic_classes import ScopeContext


_IDENTITY = {
    "owner_id": "slack/__test__",
    "actor_id": "TestFriend",
    "surface": "slack",
    "room_id": "slack/__test__",
    "room_context_id": "main",
}


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_valid_declaration_roundtrips():
    decl = {
        "visibility": "room_shared",
        "approval": {"authority_level": 30},
        "tools": {
            "allowed_tools": ["emi_team_manager"],
            "per_manager": {
                "emi_team_manager": {"allow": ["web_manager", "pod_search", "pod_fetch"]},
                "web_manager": {"block": ["http_request", "oauth_token_refresh"]},
            },
        },
        "pods": {"allowed_scopes": ["self"]},
        "writes": {"write_kg": False},
    }
    scope = load_scope(decl, identity=_IDENTITY)

    assert isinstance(scope, ScopeContext)
    assert scope.approval.authority_level == 30
    assert scope.tools.allowed_tools == ["emi_team_manager"]
    assert scope.tools.per_manager["emi_team_manager"].allow == [
        "web_manager",
        "pod_search",
        "pod_fetch",
    ]
    assert scope.tools.per_manager["web_manager"].block == [
        "http_request",
        "oauth_token_refresh",
    ]
    assert scope.pods.allowed_scopes == ["self"]
    assert scope.writes.write_kg is False


def test_loads_from_yaml_file(tmp_path):
    p = tmp_path / "scope.yaml"
    p.write_text(
        textwrap.dedent(
            """\
            visibility: room_shared
            approval:
              authority_level: 30
            tools:
              allowed_tools: [emi_team_manager]
              per_manager:
                emi_team_manager:
                  allow: [web_manager, pod_search]
            pods:
              allowed_scopes: [self]
            """
        ),
        encoding="utf-8",
    )
    scope = load_scope(p, identity=_IDENTITY)
    assert scope.tools.allowed_tools == ["emi_team_manager"]
    assert scope.tools.per_manager["emi_team_manager"].allow == ["web_manager", "pod_search"]


# ---------------------------------------------------------------------------
# fail-closed floor
# ---------------------------------------------------------------------------

def test_omitted_tools_block_is_fail_closed():
    # No tools block at all -> empty allowed surface, NOT ["all"].
    scope = load_scope({"approval": {"authority_level": 10}}, identity=_IDENTITY)
    assert scope.tools.allowed_tools == []


def test_tools_block_without_allowed_tools_is_fail_closed():
    # tools present but allowed_tools omitted -> still empty.
    scope = load_scope(
        {"tools": {"per_manager": {"web_manager": {"block": ["http_request"]}}}},
        identity=_IDENTITY,
    )
    assert scope.tools.allowed_tools == []
    # the rest of the tools block survives
    assert scope.tools.per_manager["web_manager"].block == ["http_request"]


def test_explicit_allow_list_opens_surface():
    scope = load_scope(
        {"tools": {"allowed_tools": ["emi_team_manager", "pod_search"]}},
        identity=_IDENTITY,
    )
    assert scope.tools.allowed_tools == ["emi_team_manager", "pod_search"]


def test_explicit_all_marker_opens_everything():
    scope = load_scope({"tools": {"allowed_tools": ["all"]}}, identity=_IDENTITY)
    assert scope.tools.allowed_tools == ["all"]


def test_empty_declaration_is_fully_locked_down():
    scope = load_scope({}, identity=_IDENTITY)
    assert scope.tools.allowed_tools == []
    assert scope.approval.authority_level == 0
    assert scope.pods.allowed_scopes == ["self"]
    assert scope.writes.write_kg is False


# ---------------------------------------------------------------------------
# identity stamping
# ---------------------------------------------------------------------------

def test_identity_is_stamped_from_envelope():
    scope = load_scope({"tools": {"allowed_tools": []}}, identity=_IDENTITY)
    assert scope.owner_id == "slack/__test__"
    assert scope.actor_id == "TestFriend"
    assert scope.surface == "slack"
    assert scope.room_id == "slack/__test__"
    assert scope.room_context_id == "main"


def test_scope_id_is_generated_when_absent():
    scope = load_scope({"tools": {"allowed_tools": []}}, identity=_IDENTITY)
    assert scope.scope_id.startswith("scope::slack::slack/__test__::")


def test_caller_supplied_scope_id_is_used():
    ident = {**_IDENTITY, "scope_id": "scope::explicit::123"}
    scope = load_scope({"tools": {"allowed_tools": []}}, identity=ident)
    assert scope.scope_id == "scope::explicit::123"


def test_file_authored_identity_is_ignored():
    # A file trying to spoof actor_id / owner_id must NOT win.
    decl = {
        "owner_id": "ATTACKER",
        "actor_id": "ATTACKER",
        "scope_id": "scope::spoofed",
        "tools": {"allowed_tools": []},
    }
    scope = load_scope(decl, identity=_IDENTITY)
    assert scope.owner_id == "slack/__test__"
    assert scope.actor_id == "TestFriend"
    assert scope.scope_id != "scope::spoofed"


def test_acting_as_defaults_to_user():
    scope = load_scope({"tools": {"allowed_tools": []}}, identity=_IDENTITY)
    assert scope.acting_as == "user"


def test_acting_as_from_identity():
    ident = {**_IDENTITY, "acting_as": "emi"}
    scope = load_scope({"tools": {"allowed_tools": []}}, identity=ident)
    assert scope.acting_as == "emi"


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("drop", ["owner_id", "actor_id", "surface"])
def test_missing_required_identity_raises(drop):
    ident = {k: v for k, v in _IDENTITY.items() if k != drop}
    with pytest.raises(ValueError, match="missing required field"):
        load_scope({"tools": {"allowed_tools": []}}, identity=ident)


def test_unknown_key_is_rejected():
    with pytest.raises(ValueError):
        load_scope({"bogus_field": True, "tools": {"allowed_tools": []}}, identity=_IDENTITY)


def test_mixing_all_with_names_is_rejected():
    with pytest.raises(ValueError):
        load_scope({"tools": {"allowed_tools": ["all", "pod_search"]}}, identity=_IDENTITY)


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_scope(tmp_path / "does_not_exist.yaml", identity=_IDENTITY)


def test_non_mapping_file_raises(tmp_path):
    p = tmp_path / "scope.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        load_scope(p, identity=_IDENTITY)


def test_empty_file_is_locked_down(tmp_path):
    p = tmp_path / "scope.yaml"
    p.write_text("", encoding="utf-8")
    scope = load_scope(p, identity=_IDENTITY)
    assert scope.tools.allowed_tools == []


def test_identity_must_be_mapping():
    with pytest.raises(ValueError, match="identity must be a mapping"):
        load_scope({"tools": {"allowed_tools": []}}, identity=None)  # type: ignore[arg-type]
