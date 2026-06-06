"""Tool-scope CEILING + real-room allow/block behavior.

Regression for the Telegram breach: a guest room with ``allowed_tools: []`` reached
emi_team -> personal_admin -> send_email because ``_apply_manager_narrowing`` REPLACED
the room's ``[]`` with each manager's own surface at every hop. The inherited
allow-list is a CEILING: a manager may narrow it, never widen it.

Rule:
  - parent ["all"]          -> manager's own surface stands.
  - manager_name in parent  -> parent GRANTED this manager -> its whole subtree.
  - otherwise               -> intersection. Empty parent -> [] (nothing).

The pre-existing room tests only ran ``check_tool_access`` on the RAW scope and never
drove ``_apply_manager_narrowing`` — which is exactly why the widening hid. These
tests drive the narrowing path for all three reference rooms.

Run:
  .venv\\Scripts\\python.exe -m pytest app/assistant/tests/non_agent_tests/test_scope_ceiling.py
"""
from __future__ import annotations

import yaml

from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.lib.tool_execution.tool_access_control import check_tool_access
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.manager_runtime.services.tool_scope_service import ToolScopeService
from app.assistant.rooms.room_resource_loader import resolve_room_config_dir
from app.assistant.scope.loader import load_scope
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ScopeContext, ScopeToolPolicy


# ── helpers ──────────────────────────────────────────────────────────────────

def _manager_cfg_from_disk(name: str) -> dict:
    p = get_repo_root() / "app" / "assistant" / "multi_agents" / name / "config.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _load_room(rid: str, surface: str) -> ScopeContext:
    return load_scope(
        resolve_room_config_dir(rid) / "scope.yaml",
        identity={"owner_id": rid, "actor_id": "U_x", "surface": surface,
                  "room_id": rid, "room_context_id": "main"},
    )


def _scope(allowed) -> ScopeContext:
    return ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="ui", room_id="r",
        tools=ScopeToolPolicy(allowed_tools=allowed),
    )


def _narrow(scope: ScopeContext, manager_name: str, manager_config: dict) -> ScopeContext:
    return ScopeAdapter()._apply_manager_narrowing(
        scope=scope, manager_name=manager_name, manager_config=manager_config)


def _can_use(scope: ScopeContext, tool: str) -> bool:
    ok, _ = check_tool_access(
        tool_name=tool, scope_contract_enforced=True, scope_context=scope,
        task_allowed_tools=None, task_except_tools=None, caller_name="t")
    return ok


# A manager whose own contract declares a calendar/email surface.
_CFG = {"scope_contract": {"tools": {"allowed_tools":
        ["send_email", "get_calendar_events", "personal_admin_manager"]}}}


# ── 1. the four ceiling cases ────────────────────────────────────────────────

def test_ceiling_all_keeps_manager_surface():
    out = _narrow(_scope(["all"]), "m", _CFG)
    assert set(out.tools.allowed_tools) == {"send_email", "get_calendar_events", "personal_admin_manager"}

def test_ceiling_named_manager_grants_its_subtree():
    out = _narrow(_scope(["m"]), "m", _CFG)            # m named -> full subtree
    assert set(out.tools.allowed_tools) == {"send_email", "get_calendar_events", "personal_admin_manager"}

def test_ceiling_empty_parent_reaches_nothing():
    out = _narrow(_scope([]), "m", _CFG)               # THE BREACH FIX
    assert out.tools.allowed_tools == []

def test_ceiling_unnamed_manager_intersects_down():
    out = _narrow(_scope(["personal_admin_manager"]), "m", _CFG)  # m not named -> intersect
    assert out.tools.allowed_tools == ["personal_admin_manager"]


# ── 2. telegram: allow:[] reaches NOTHING through the entry hop ──────────────

def test_telegram_entry_hop_reaches_nothing():
    s = _load_room("telegram/8561764764", "telegram")
    assert s.tools.allowed_tools == []
    narrowed = _narrow(s, "room_manager", _manager_cfg_from_disk("room_manager"))
    assert narrowed.tools.allowed_tools == [], "room_manager widened telegram's [] (the breach)"
    for tool in ("send_email", "emi_team_manager", "personal_admin_manager", "create_calendar_event"):
        assert _can_use(narrowed, tool) is False, f"{tool} must be denied for the telegram guest"


# ── 3. master_room: allow:[all] reaches everything (incl. send_email via chain) ─

def test_master_entry_hop_keeps_full_surface():
    s = _load_room("master_room", "ui")
    assert s.tools.allowed_tools == ["all"]
    rm = _narrow(s, "room_manager", _manager_cfg_from_disk("room_manager"))
    assert _can_use(rm, "emi_team_manager") is True
    assert _can_use(rm, "send_email") is True            # room_manager exposes it directly

def test_master_reaches_personal_admin_through_emi_team_chain():
    s = _load_room("master_room", "ui")
    rm = _narrow(s, "room_manager", _manager_cfg_from_disk("room_manager"))
    # emi_team_manager IS named in rm.allowed -> entering it grants its subtree.
    et = _narrow(rm, "emi_team_manager", _manager_cfg_from_disk("emi_team_manager"))
    assert _can_use(et, "personal_admin_manager") is True
    # ...and inside personal_admin, send_email is reachable.
    pa = _narrow(et, "personal_admin_manager", _manager_cfg_from_disk("personal_admin_manager"))
    assert _can_use(pa, "send_email") is True


# ── 4. slack: allow:[all] + per_manager narrows the switchboard to emi_team ──

_SLACK_UNIVERSE = {n: {} for n in [
    "emi_team_manager", "personal_admin_manager", "web_manager", "bash_manager",
    "send_email", "search_web", "scrape_url", "http_request", "pod_search",
    "find_tool", "install_tool", "ask_user",
]}


class _FakeRegistry:
    def get_all_tools(self): return dict(_SLACK_UNIVERSE)
    def get_tool_descriptions(self, names): return {n: n for n in names}
    def get_tool(self, name): return _SLACK_UNIVERSE.get(name)


def _visible(scope: ScopeContext, manager_name: str) -> list[str]:
    """Tools the manager's planner is SHOWN under the given (already-narrowed)
    scope. Visibility is a strict subset of what the scope ALLOWS — it derives
    from allowed_tools, it does not re-derive per_manager."""
    bb = Blackboard()
    bb.update_state_value("scope_context", scope.model_dump())
    bb.update_state_value("scope_contract_enforced", True)
    bb.update_state_value("recently_used_tools", [])
    bb.update_state_value("recently_installed_tools", [])
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeRegistry(),
        manager_config={"name": manager_name, "tool_visibility": {"use_narrower": False}},
        task="do something", information="")
    raw = bb.get_state_value("visible_tools")
    return list(raw) if isinstance(raw, list) else []


def test_slack_switchboard_only_routes_to_emi_team():
    s = _load_room("slack/C08AB0R54HM", "slack")
    assert s.tools.allowed_tools == ["emi_team_manager"]   # the ceiling, on disk
    sw = _narrow(s, "room_manager", _manager_cfg_from_disk("room_manager"))
    # EXECUTION: the switchboard can reach emi_team_manager and nothing else.
    assert _can_use(sw, "emi_team_manager") is True
    for denied in ("personal_admin_manager", "send_email", "bash_manager", "create_calendar_event"):
        assert _can_use(sw, denied) is False, f"{denied} must be denied at the slack switchboard"
    # VISIBILITY is a subset of ALLOW.
    vis = _visible(sw, "room_manager")
    assert "emi_team_manager" in vis
    assert "personal_admin_manager" not in vis
    assert "send_email" not in vis
    assert set(vis) <= set(sw.tools.allowed_tools)


def test_slack_emi_team_narrowed_to_web_research():
    s = _load_room("slack/C08AB0R54HM", "slack")
    # The real path: switchboard -> emi_team. emi_team is GRANTED its subtree,
    # then per_manager[emi_team] folds it down to the research surface.
    et = _narrow(_narrow(s, "room_manager", _manager_cfg_from_disk("room_manager")),
                 "emi_team_manager", _manager_cfg_from_disk("emi_team_manager"))
    # EXECUTION: research surface reachable; owner-data + credential tools denied.
    assert _can_use(et, "web_manager") is True
    assert _can_use(et, "pod_search") is True
    for denied in ("personal_admin_manager", "send_email", "bash_manager", "http_request"):
        assert _can_use(et, denied) is False, f"{denied} must be denied for the slack emi_team hop"
    # VISIBILITY subset of ALLOW.
    vis = _visible(et, "emi_team_manager")
    assert "web_manager" in vis
    assert "pod_search" in vis
    assert "personal_admin_manager" not in vis   # the key restriction
    assert set(vis) <= set(et.tools.allowed_tools)
