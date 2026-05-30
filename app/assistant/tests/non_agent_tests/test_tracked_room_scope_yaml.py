"""Tracked Path-A room scope.yaml migrations (Step 2 rollout).

Covers the 5 tracked rooms that go through room_scope_builder:
- 1234 (sms)      -> LOCKED DOWN (allowed_tools: []) — sms never used as an
                     action surface. Tools are the ONLY deliberate change;
                     everything else stays faithful.
- doc_editor (ui 50), emi_code_room (ui 60), kg_dev_room (ui 50),
  task_create (ui 50) -> faithful migrations (full tool surface preserved).

IMPORTANT — non-circular by design: this asserts the loaded scope.yaml against
EXPLICIT GOLDEN VALUES captured from the pre-migration builder, NOT against the
live builder. The overlay is now live, so the builder reads scope.yaml back —
comparing to it would be circular and would pass even on a wrong file. The
golden values below are the source of truth.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_tracked_room_scope_yaml.py
"""
from __future__ import annotations

import pytest

from app.assistant.lib.tool_execution.tool_access_control import check_tool_access
from app.assistant.rooms.room_resource_loader import resolve_room_config_dir
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.scope.loader import load_scope

# Golden permission values captured from the builder BEFORE any scope.yaml
# existed (the clean baseline). Each room's scope.yaml must reproduce these.
# tools.allowed_tools for 1234 is the one deliberate deviation: [all] -> [].
_GOLDEN = {
    "1234": dict(
        surface="sms", authority=40, allowed_tools=[],  # LOCKDOWN (was [all])
        external=False, per_manager={},
        resources=["resource_chat_guidelines", "resource_user_data",
                   "resource_assistant_data", "resource_user_email"],
        resource_groups=["chat"], entity_cards=["all"],
        write_kg=False, allow_fact_extraction=False,
        auto_send=True, allow_initiation=False,
    ),
    "doc_editor": dict(
        surface="ui", authority=50, allowed_tools=["all"],
        external=True, per_manager={},      # doc_editor: external_action true
        resources=["all"], resource_groups=[], entity_cards=[],
        write_kg=False, allow_fact_extraction=False,
        auto_send=True, allow_initiation=False,
    ),
    "emi_code_room": dict(
        surface="ui", authority=60, allowed_tools=["all"],
        external=False, per_manager={},
        resources=["all"], resource_groups=[], entity_cards=[],
        write_kg=False, allow_fact_extraction=False,
        auto_send=True, allow_initiation=False,
    ),
    "kg_dev_room": dict(
        surface="ui", authority=50, allowed_tools=["all"],
        external=False, per_manager={},     # kg_dev: external_action FALSE
        resources=["all"], resource_groups=[], entity_cards=[],
        write_kg=False, allow_fact_extraction=False,
        auto_send=True, allow_initiation=False,
    ),
    "task_create": dict(
        surface="ui", authority=50, allowed_tools=["all"],
        external=False, per_manager={},
        resources=["all"], resource_groups=[], entity_cards=[],
        write_kg=False, allow_fact_extraction=False,
        auto_send=True, allow_initiation=False,
    ),
}


def _load(rid: str):
    g = _GOLDEN[rid]
    return load_scope(
        resolve_room_config_dir(rid) / "scope.yaml",
        identity={"owner_id": rid, "actor_id": "U_x", "surface": g["surface"],
                  "room_id": rid, "room_context_id": "main"},
    )


def test_all_scope_files_exist():
    for rid in _GOLDEN:
        assert (resolve_room_config_dir(rid) / "scope.yaml").exists(), f"missing scope.yaml for {rid}"


@pytest.mark.parametrize("rid", list(_GOLDEN))
def test_room_matches_golden(rid):
    g = _GOLDEN[rid]
    s = _load(rid)
    assert s.approval.authority_level == g["authority"], f"{rid} authority"
    assert s.tools.allowed_tools == g["allowed_tools"], f"{rid} allowed_tools"
    assert s.tools.allow_external_side_effects == g["external"], f"{rid} external_side_effects"
    assert {k: (r.allow, r.block) for k, r in s.tools.per_manager.items()} == g["per_manager"], f"{rid} per_manager"
    assert s.resources.allowed_global_resources == g["resources"], f"{rid} resources"
    assert s.resources.resource_groups == g["resource_groups"], f"{rid} resource_groups"
    assert s.entities.allowed_entity_cards == g["entity_cards"], f"{rid} entity_cards"
    assert s.writes.write_kg == g["write_kg"], f"{rid} write_kg"
    assert s.writes.allow_fact_extraction == g["allow_fact_extraction"], f"{rid} allow_fact_extraction"
    assert s.delivery.auto_send == g["auto_send"], f"{rid} auto_send"
    assert s.delivery.allow_initiation == g["allow_initiation"], f"{rid} allow_initiation"


def test_sms_no_tools_enforced_fail_closed():
    s = _load("1234")
    data = ScopeAdapter()._project_scope_to_runtime_data(base_data={}, scope=s)
    assert data["task_allowed_tools"] == []
    for tool in ("send_email", "create_todo_task", "nest_home_control"):
        allowed, reason = check_tool_access(
            tool_name=tool, scope_contract_enforced=True, scope_context=s,
            task_allowed_tools=data["task_allowed_tools"],
            task_except_tools=data["task_except_tools"], caller_name="test_sms",
        )
        assert allowed is False, f"{tool} should be denied on sms (got {reason!r})"


def test_ui_rooms_tools_pass_execution_check():
    for rid in ("doc_editor", "emi_code_room", "kg_dev_room", "task_create"):
        s = _load(rid)
        data = ScopeAdapter()._project_scope_to_runtime_data(base_data={}, scope=s)
        allowed, _ = check_tool_access(
            tool_name="emi_team_manager", scope_contract_enforced=True, scope_context=s,
            task_allowed_tools=data["task_allowed_tools"],
            task_except_tools=data["task_except_tools"], caller_name=f"test_{rid}",
        )
        assert allowed is True, f"{rid} should permit tools ([all])"
