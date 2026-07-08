"""slack/__test__ scope.yaml verification (Step 2a, hardened).

ORIGINALLY this file compared load_scope(scope.yaml) to the room builder output.
That was sound when written — but Step 2b made the scope.yaml overlay live, so
the builder now reads slack/__test__/scope.yaml back. A builder-vs-loader
comparison became CIRCULAR (tautological): it passed for ANY scope.yaml content
and could not catch a drifted file. The circularity audit (2026-05-30) caught
this with a mutation test.

Rewritten to assert load_scope(scope.yaml) against EXPLICIT GOLDEN CONSTANTS
(the same non-circular pattern as test_tracked_room_scope_yaml.py). The golden
values are the intended slack/__test__ permission bucket (mirrors slack_standard
at authority 30). A wrong/drifted scope.yaml now fails.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_scope_yaml_matches_room_builder.py
"""
from __future__ import annotations

from app.assistant.rooms.room_resource_loader import resolve_room_config_dir
from app.assistant.scope.loader import load_scope


_ROOM_ID = "slack/__test__"

# Identity envelope (stamped at load; not part of the permission assertions).
# owner_id models the room builder's canonical value: the primary human
# principal, never the room id (2026-07-07 scope audit).
_IDENTITY = {
    "owner_id": "user",
    "actor_id": "U_test",
    "surface": "slack",
    "room_id": _ROOM_ID,
    "room_context_id": "main",
    "visibility": "room_shared",
    "policy_id": "room_policy::slack/__test__::v1",
    "acting_as": "user",
    "reply_to": None,
}

# GOLDEN: the intended slack/__test__ permission bucket. Hardcoded literals, NOT
# derived from the builder — so a drifted scope.yaml is caught.
_EMI_TEAM_ALLOW = [
    "web_manager", "pod_search", "pod_fetch", "ask_user", "find_tool",
    "install_tool", "read_skill", "discover_skills", "read_tool_result",
]
_WEB_BLOCK = ["http_request", "oauth_token_refresh"]
_RESOURCES = [
    "resource_user_data", "resource_assistant_data", "resource_chat_guidelines",
    "resource_assistant_personality", "resource_weather",
]


def _loaded():
    return load_scope(resolve_room_config_dir(_ROOM_ID) / "scope.yaml", identity=_IDENTITY)


# ---------------------------------------------------------------------------
# permission bucket — asserted against hardcoded golden constants
# ---------------------------------------------------------------------------

def test_scope_yaml_exists():
    assert (resolve_room_config_dir(_ROOM_ID) / "scope.yaml").exists()


def test_authority_is_30():
    assert _loaded().approval.authority_level == 30


def test_tools_allowed_tools():
    assert _loaded().tools.allowed_tools == ["all"]


def test_per_manager_rules():
    pm = _loaded().tools.per_manager
    assert pm["emi_team_manager"].allow == _EMI_TEAM_ALLOW
    assert pm["emi_team_manager"].block == []
    assert pm["web_manager"].allow is None
    assert pm["web_manager"].block == _WEB_BLOCK


def test_pods():
    assert _loaded().pods.allowed_scopes == ["self"]


def test_resources():
    s = _loaded()
    assert s.resources.allowed_global_resources == _RESOURCES
    assert s.resources.resource_groups == ["chat", "memory"]


def test_entities():
    assert _loaded().entities.allowed_entity_cards == ["all"]


def test_writes():
    w = _loaded().writes
    assert w.write_unified_log is True
    assert w.write_kg is False
    assert w.allow_fact_extraction is False


def test_delivery_permission_bits():
    d = _loaded().delivery
    assert d.auto_send is True
    assert d.allow_initiation is True


# ---------------------------------------------------------------------------
# identity stamping (sound — pins identity against the envelope, not the file)
# ---------------------------------------------------------------------------

def test_identity_stamped():
    s = _loaded()
    assert s.owner_id == "user"
    assert s.actor_id == "U_test"
    assert s.surface == "slack"
    assert s.visibility == "room_shared"
    assert s.acting_as == "user"
