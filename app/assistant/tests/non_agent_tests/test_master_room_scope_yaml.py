"""master_room scope.yaml migration (Step 2 rollout).

Proves the scope.yaml overlay produces the SAME permission bucket the legacy
builder emits for master_room (authority 99, full surface, all pods, KG writes,
external side effects) AND preserves builder-computed room-behavior + identity.

master_room is the highest-stakes room (authority 99), so this asserts the
permissive-but-explicit fields are NOT silently downgraded by the fail-closed
loader floor.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_master_room_scope_yaml.py
"""
from __future__ import annotations

import pytest

from app.assistant.rooms.room_resource_loader import (
    load_room_context_for_manager,
    resolve_room_config_dir,
)
from app.assistant.room_session_manager.services.room_scope_builder import (
    build_scope_contract_for_room_request,
)
from app.assistant.scope.loader import load_scope
from app.assistant.tests.room_test_harness.harness import _build_envelope
from app.assistant.utils.pydantic_classes import ScopeContext


_ROOM_ID = "master_room"


def _golden() -> dict:
    """What the builder emits for master_room (now via the overlay, since the
    scope.yaml exists). Permission must equal the pre-migration values."""
    room_ctx = load_room_context_for_manager(_ROOM_ID)
    env = _build_envelope(
        surface="ui", room_id=_ROOM_ID, content="hi",
        speaker_name="Jukka", speaker_external_id="U_jukka", context_id="main",
    )
    return build_scope_contract_for_room_request(
        room_ctx=room_ctx, envelope=env,
        request_data={"task_allowed_tools": None, "task_except_tools": None,
                      "reply_to": None, "actas_principal": "user"},
    )


@pytest.fixture(scope="module")
def built() -> dict:
    return _golden()


def test_scope_yaml_exists():
    assert (resolve_room_config_dir(_ROOM_ID) / "scope.yaml").exists()


# --- the permissive fields that MUST be explicit (fail-closed floor would
#     otherwise downgrade them) ---

def test_authority_is_99(built):
    assert built["approval"]["authority_level"] == 99


def test_full_tool_surface(built):
    assert built["tools"]["allowed_tools"] == ["all"]
    assert built["tools"]["allow_external_side_effects"] is True
    # master_room is full-trust ([all]); the ONE per_manager rule is a
    # narrower-cost reduction (NOT a capability cut): emi_team_manager's DIRECT
    # pool is pinned to its delegate-managers + ask_kg + operational tools so
    # its LLM tool_narrower ranks ~16 instead of ~138. Specialist leaves stay
    # reachable THROUGH those managers. See rooms/master_room/scope.yaml header.
    per_mgr = built["tools"]["per_manager"]
    assert set(per_mgr.keys()) == {"emi_team_manager"}
    allow = per_mgr["emi_team_manager"]["allow"]
    # the 7 delegate managers + ask_kg + emi_team operational tools (incl. the
    # pod read/write trio pod_search/pod_fetch/mint_pod and the work-object
    # read pair search_work_objects/read_work_object, 829ca1ec) = 18 total.
    assert "web_manager" in allow and "http_manager" in allow
    assert "personal_admin_manager" in allow and "ask_kg" in allow
    assert "pod_search" in allow and "ask_user" in allow
    assert "mint_pod" in allow  # emi_team can persist a content pod directly
    assert "search_work_objects" in allow and "read_work_object" in allow
    assert "kg_query_manager" not in allow  # retired (superseded by ask_kg)
    assert "create_dayflow_ticket" not in allow  # switchboard-only, not emi_team's
    assert len(allow) == 18


def test_all_pods_visible(built):
    assert built["pods"]["allowed_scopes"] == ["all"]


def test_kg_writes_enabled(built):
    assert built["writes"]["write_unified_log"] is True
    assert built["writes"]["write_kg"] is True
    assert built["writes"]["allow_fact_extraction"] is True


def test_resources_and_entities_wide_open(built):
    assert built["resources"]["allowed_global_resources"] == ["all"]
    assert built["resources"]["resource_groups"] == ["chat", "memory"]
    assert built["entities"]["allowed_entity_cards"] == ["all"]


def test_delivery_permission_bits(built):
    assert built["delivery"]["auto_send"] is True
    assert built["delivery"]["allow_initiation"] is True
    assert built["delivery"]["allowed_reply_types"] == ["ui"]  # derived, preserved


# --- room-behavior + identity preserved ---

def test_room_behavior_preserved(built):
    assert built["history"]["mode"] == "summary_plus_recent"
    assert built["retention"]["persist_tool_results"] is True
    assert built["surface"] == "ui"
    # owner_id = the primary human principal (whose data), NOT the room id —
    # the room lives in room_id. Canonical rule from the 2026-07-07 scope audit.
    assert built["owner_id"] == "user"
    assert built["actor_id"] == "U_jukka"
    assert built["visibility"] == "owner_only"


# NOTE: a former test_full_permission_bucket_matches_loader was REMOVED
# (circularity audit 2026-05-30). It compared load_scope(scope.yaml) to the
# builder output, but the overlay makes the builder read scope.yaml back — so
# it was tautological and caught no drift. The per-field tests above pin every
# permission value against hardcoded literals on the builder output, which DO
# catch a wrong scope.yaml (e.g. authority 50 -> test_authority_is_99 fails).
