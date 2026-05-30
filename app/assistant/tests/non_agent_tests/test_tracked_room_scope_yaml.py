"""Tracked Path-A room scope.yaml migrations (Step 2 rollout).

Covers the 5 tracked rooms that go through room_scope_builder:
- 1234 (sms)      -> LOCKED DOWN (allowed_tools: []) — sms never used as an
                     action surface; corrects the legacy [all] default.
- doc_editor (ui 50), emi_code_room (ui 60), kg_dev_room (ui 50),
  task_create (ui 50) -> faithful migrations (full tool surface preserved).

For the faithful (ui) rooms, asserts the scope.yaml overlay output equals the
permission bucket the builder emits. For sms, asserts the corrected lock-down
AND that it is enforced fail-closed at check_tool_access.

(Katy / personal rooms are tested separately and locally — those room dirs are
gitignored.)

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_tracked_room_scope_yaml.py
"""
from __future__ import annotations

import pytest

from app.assistant.lib.tool_execution.tool_access_control import check_tool_access
from app.assistant.rooms.room_resource_loader import (
    load_room_context_for_manager,
    resolve_room_config_dir,
)
from app.assistant.room_session_manager.services.room_scope_builder import (
    build_scope_contract_for_room_request,
)
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.scope.loader import load_scope
from app.assistant.tests.e2e.harness import _build_envelope
from app.assistant.utils.pydantic_classes import ScopeContext

# room_id -> surface
_FAITHFUL = {"doc_editor": "ui", "emi_code_room": "ui", "kg_dev_room": "ui", "task_create": "ui"}
_SMS_ROOM = "1234"

_PERMISSION_BLOCKS = ("tools", "pods", "resources", "entities", "cards", "writes", "approval")


def _built(rid: str, surface: str) -> dict:
    ctx = load_room_context_for_manager(rid)
    env = _build_envelope(surface=surface, room_id=rid, content="hi",
                          speaker_name="X", speaker_external_id="U_x", context_id="main")
    return build_scope_contract_for_room_request(
        room_ctx=ctx, envelope=env,
        request_data={"task_allowed_tools": None, "task_except_tools": None,
                      "reply_to": None, "actas_principal": "user"},
    )


def test_all_scope_files_exist():
    for rid in (*_FAITHFUL, _SMS_ROOM):
        assert (resolve_room_config_dir(rid) / "scope.yaml").exists(), f"missing scope.yaml for {rid}"


@pytest.mark.parametrize("rid,surface", list(_FAITHFUL.items()))
def test_faithful_room_permission_bucket_matches_builder(rid, surface):
    built = _built(rid, surface)
    identity = {k: built.get(k) for k in (
        "scope_id", "owner_id", "actor_id", "surface", "room_id",
        "room_context_id", "visibility", "policy_id", "reply_to", "acting_as",
    )}
    loaded = load_scope(resolve_room_config_dir(rid) / "scope.yaml", identity=identity).model_dump()
    built_ctx = ScopeContext.model_validate(built).model_dump()
    for block in _PERMISSION_BLOCKS:
        assert loaded[block] == built_ctx[block], f"{rid}: permission block '{block}' drifted"
    assert loaded["delivery"]["auto_send"] == built_ctx["delivery"]["auto_send"]
    assert loaded["delivery"]["allow_initiation"] == built_ctx["delivery"]["allow_initiation"]


def test_kg_dev_keeps_external_side_effects():
    # kg_dev is the one UI room with allow_external_side_effects: true — guard
    # against a faithful-migration regression that would silently flip it.
    built = _built("kg_dev_room", "ui")
    assert built["tools"]["allow_external_side_effects"] is True


def test_authorities_preserved():
    assert _built("doc_editor", "ui")["approval"]["authority_level"] == 50
    assert _built("emi_code_room", "ui")["approval"]["authority_level"] == 60
    assert _built("kg_dev_room", "ui")["approval"]["authority_level"] == 50
    assert _built("task_create", "ui")["approval"]["authority_level"] == 50


# --- sms: corrected lock-down ---

def test_sms_is_locked_down():
    built = _built(_SMS_ROOM, "sms")
    assert built["tools"]["allowed_tools"] == []          # corrected from legacy [all]
    assert built["approval"]["authority_level"] == 30


def test_sms_no_tools_enforced_fail_closed():
    s = load_scope(resolve_room_config_dir(_SMS_ROOM) / "scope.yaml", identity={
        "owner_id": _SMS_ROOM, "actor_id": "U_sms", "surface": "sms",
        "room_id": _SMS_ROOM, "room_context_id": "main",
    })
    data = ScopeAdapter()._project_scope_to_runtime_data(base_data={}, scope=s)
    assert data["task_allowed_tools"] == []
    for tool in ("send_email", "create_todo_task", "nest_home_control"):
        allowed, reason = check_tool_access(
            tool_name=tool, scope_contract_enforced=True, scope_context=s,
            task_allowed_tools=data["task_allowed_tools"],
            task_except_tools=data["task_except_tools"], caller_name="test_sms",
        )
        assert allowed is False, f"{tool} should be denied on sms (got {reason!r})"
