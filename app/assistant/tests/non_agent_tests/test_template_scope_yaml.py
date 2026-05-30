"""Template scope.yaml migration (Step 2 rollout): slack_standard + telegram_standard.

slack_standard: faithful no-op — scope.yaml reproduces the builder's permission
bucket (authority 70, [all] + per_manager research-narrowing).

telegram_standard: deliberate correction — NO tools (allowed_tools: []). The
legacy ROOM.md permissions.allowed_tools field was never read by the builder, so
live Telegram silently ran with the FULL surface; scope.yaml makes the lock-down
real. Proven end-to-end: the projected task_allowed_tools [] makes
check_tool_access deny every tool (fail-closed at the execution boundary).

Also asserts room_bootstrap copies scope.yaml into new rooms (not just ROOM.md).

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_template_scope_yaml.py
"""
from __future__ import annotations

from pathlib import Path

from app.assistant.lib.tool_execution.tool_access_control import check_tool_access
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.rooms.room_bootstrap import _TEMPLATE_FILES
from app.assistant.scope.loader import load_scope

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "rooms" / "_templates"
_SLACK = _TEMPLATES_DIR / "slack_standard" / "scope.yaml"
_TELEGRAM = _TEMPLATES_DIR / "telegram_standard" / "scope.yaml"

_IDENTITY = {
    "owner_id": "slack/Cxxx",
    "actor_id": "U_friend",
    "surface": "slack",
    "room_id": "slack/Cxxx",
    "room_context_id": "main",
    "visibility": "room_shared",
}


# ---------------------------------------------------------------------------
# files exist + bootstrap copies them
# ---------------------------------------------------------------------------

def test_template_scope_files_exist():
    assert _SLACK.exists(), f"missing {_SLACK}"
    assert _TELEGRAM.exists(), f"missing {_TELEGRAM}"


def test_bootstrap_copies_scope_yaml():
    # Without this, new rooms would get ROOM.md but never scope.yaml.
    assert "scope.yaml" in _TEMPLATE_FILES
    assert "ROOM.md" in _TEMPLATE_FILES


# ---------------------------------------------------------------------------
# slack_standard — faithful to the builder's permission bucket
# ---------------------------------------------------------------------------

def test_slack_template_permission():
    s = load_scope(_SLACK, identity=_IDENTITY)
    assert s.approval.authority_level == 70
    assert s.tools.allowed_tools == ["all"]
    assert s.tools.allow_external_side_effects is True
    assert s.tools.per_manager["emi_team_manager"].allow[:3] == [
        "web_manager", "pod_search", "pod_fetch",
    ]
    assert s.tools.per_manager["web_manager"].block == ["http_request", "oauth_token_refresh"]
    assert s.pods.allowed_scopes == ["self"]
    assert s.writes.write_kg is True
    assert s.writes.allow_fact_extraction is True
    assert "resource_weather" in s.resources.allowed_global_resources
    assert s.resources.resource_groups == ["chat", "memory"]
    assert s.entities.allowed_entity_cards == ["all"]
    assert s.delivery.auto_send is True
    assert s.delivery.allow_initiation is True


# ---------------------------------------------------------------------------
# telegram_standard — NO tools (the corrected behavior)
# ---------------------------------------------------------------------------

def _telegram_identity():
    return {
        "owner_id": "telegram/123",
        "actor_id": "U_tg",
        "surface": "telegram",
        "room_id": "telegram/123",
        "room_context_id": "main",
        "visibility": "room_shared",
    }


def test_telegram_template_has_no_tools():
    s = load_scope(_TELEGRAM, identity=_telegram_identity())
    assert s.tools.allowed_tools == []          # the whole point
    assert s.tools.allow_external_side_effects is False
    assert s.tools.per_manager == {}
    assert s.approval.authority_level == 40
    assert s.pods.allowed_scopes == ["self"]
    assert s.writes.write_kg is False
    assert s.writes.allow_fact_extraction is False
    assert s.delivery.allow_initiation is False


def test_telegram_no_tools_is_enforced_fail_closed():
    """End-to-end: [] -> projected task_allowed_tools [] -> check_tool_access denies.

    This is the load-bearing security property: even though the VISIBILITY filter
    (tool_scope_service) treats an empty allow_set as falsy and skips filtering,
    the EXECUTION gate (check_tool_access Layer 1, scope_context) denies every
    tool. So no tool can run on a telegram chat regardless of what the planner
    is shown.
    """
    s = load_scope(_TELEGRAM, identity=_telegram_identity())
    data = ScopeAdapter()._project_scope_to_runtime_data(base_data={}, scope=s)
    assert data["task_allowed_tools"] == []

    for tool in ("create_todo_task", "send_email", "nest_home_control", "web_manager"):
        allowed, reason = check_tool_access(
            tool_name=tool,
            scope_contract_enforced=True,
            scope_context=s,
            task_allowed_tools=data["task_allowed_tools"],
            task_except_tools=data["task_except_tools"],
            caller_name="test_telegram",
        )
        assert allowed is False, f"{tool} should be denied on telegram (got {reason!r})"
        assert "allowed_tools" in reason  # "...outside scope_contract allowed_tools."


def test_slack_tools_still_pass_execution_check():
    """Contrast: slack's [all] lets tools through the execution check."""
    s = load_scope(_SLACK, identity=_IDENTITY)
    data = ScopeAdapter()._project_scope_to_runtime_data(base_data={}, scope=s)
    allowed, reason = check_tool_access(
        tool_name="web_manager",
        scope_contract_enforced=True,
        scope_context=s,
        task_allowed_tools=data["task_allowed_tools"],
        task_except_tools=data["task_except_tools"],
        caller_name="test_slack",
    )
    assert allowed is True, f"web_manager should pass on slack (got {reason!r})"
