"""Tests for ScopeToolPolicy.per_manager — per-manager allow/block rules.

These rules fire when a specific manager runs in the scope's call tree. As of
the scope-ceiling consolidation they are folded into the scope's allowed_tools /
blocked_tools at NARROWING time (ScopeAdapter._apply_manager_narrowing), so they
bind at EXECUTION (check_tool_access), not merely at visibility:

- rule.allow (when not None) intersects the manager's ceiling with rule.allow
  (replacing it when the ceiling is "all").
- rule.block subtracts items and unions them into blocked_tools, so the denial
  propagates to children.

Unmentioned managers run with their ceiling unchanged. The visibility layer
(tool_scope_service) then derives a subset from the narrowed allowed_tools — it
no longer re-derives per_manager itself, so visibility ⊆ allow always holds.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.lib.tool_execution.tool_access_control import check_tool_access
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.manager_runtime.services.tool_scope_service import ToolScopeService
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeToolPolicy,
    ScopeToolRule,
)


# ---------------------------------------------------------------------------
# Fixtures: tiny fake tool_registry with a fixed tool universe
# ---------------------------------------------------------------------------

_FAKE_TOOLS = {
    "emi_team_manager": {},
    "web_manager": {},
    "personal_admin_manager": {},
    "playwright_manager": {},
    "bash_manager": {},
    "search_web": {},
    "scrape_url": {},
    "http_request": {},
    "oauth_token_refresh": {},
    "pod_search": {},
    "pod_fetch": {},
    "ask_user": {},
    "find_tool": {},
    "install_tool": {},
}


class _FakeToolRegistry:
    def get_all_tools(self):
        return dict(_FAKE_TOOLS)

    def get_tool_descriptions(self, names):
        return {n: f"desc({n})" for n in names}

    def get_tool(self, name):
        return _FAKE_TOOLS.get(name)


def _narrow(scope: ScopeContext, manager_name: str) -> ScopeContext:
    """Fold the scope's per_manager[manager_name] rule into allowed_tools, the
    way every manager invocation does before tool-scope/visibility runs. The
    manager declares no scope_contract of its own here, so ONLY per_manager
    (and the inherited ceiling) shapes the result."""
    return ScopeAdapter()._apply_manager_narrowing(
        scope=scope, manager_name=manager_name, manager_config={})


def _bb_with_scope(scope: ScopeContext) -> Blackboard:
    bb = Blackboard()
    bb.update_state_value("scope_context", scope.model_dump())
    bb.update_state_value("scope_contract_enforced", True)
    bb.update_state_value("recently_used_tools", [])
    bb.update_state_value("recently_installed_tools", [])
    return bb


def _manager_cfg(name: str, always_show=None) -> dict:
    return {
        "name": name,
        "tool_visibility": {
            "always_show": always_show or ["find_tool", "install_tool", "ask_user"],
            # use_narrower=False keeps the test fully deterministic.
            "use_narrower": False,
        },
    }


def _read_visible(bb: Blackboard) -> list[str]:
    raw = bb.get_state_value("visible_tools")
    return list(raw) if isinstance(raw, list) else []


def _can_use(scope: ScopeContext, tool: str) -> bool:
    ok, _ = check_tool_access(
        tool_name=tool, scope_contract_enforced=True, scope_context=scope,
        task_allowed_tools=None, task_except_tools=None, caller_name="t")
    return ok


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------

class _CfgRegistry:
    """Minimal registry whose get_all_tools returns tool configs with contracts,
    so resolve_tool_min_authority can read a real min_authority floor."""
    def __init__(self, cfg):
        self._cfg = cfg

    def get_all_tools(self):
        return dict(self._cfg)


# ask_kg-like tool: read-only but min_authority 90 (the personal-data floor).
_ASK_KG_CFG = {
    "ask_kg": {"tool_contract": {"metadata": {"min_authority": 90}}},
    "ask_user": {"tool_contract": {"metadata": {"min_authority": 0}}},
}


def _bb_pin(scope: ScopeContext, pinned: list) -> Blackboard:
    bb = _bb_with_scope(scope)
    bb.update_state_value("visible_tools", pinned)
    return bb


def test_pinned_tool_above_authority_is_not_visible():
    """A forced/pinned tool whose min_authority exceeds the scope's authority
    must be dropped — the pinned path enforces the ceiling, not just relevance.
    (Regression for the bypass that aborted emi_team_manager via ask_kg.)"""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="internal",
        tools=ScopeToolPolicy(allowed_tools=["all"]),
        approval=ScopeApprovalPolicy(authority_level=0),
    )
    bb = _bb_pin(scope, ["ask_kg"])
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_CfgRegistry(_ASK_KG_CFG),
        manager_config=_manager_cfg("x"), task="t", information="")
    assert "ask_kg" not in _read_visible(bb)   # authority 0 < floor 90 -> hidden


def test_pinned_tool_visible_when_authority_sufficient():
    """authority_level >= the tool's floor AND in allowed_tools -> pinned tool
    is visible (the reasoning_agent fix: authority 99 + allowed ask_kg)."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="internal",
        tools=ScopeToolPolicy(allowed_tools=["ask_kg"]),
        approval=ScopeApprovalPolicy(authority_level=99),
    )
    bb = _bb_pin(scope, ["ask_kg"])
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_CfgRegistry(_ASK_KG_CFG),
        manager_config=_manager_cfg("x"), task="t", information="")
    assert _read_visible(bb) == ["ask_kg"]


def test_pinned_tool_not_in_allowed_is_dropped():
    """A pinned tool outside allowed_tools is dropped even with full authority —
    pinned visibility is a subset of the allow ceiling."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="internal",
        tools=ScopeToolPolicy(allowed_tools=["ask_user"]),
        approval=ScopeApprovalPolicy(authority_level=100),
    )
    bb = _bb_pin(scope, ["ask_kg"])
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_CfgRegistry(_ASK_KG_CFG),
        manager_config=_manager_cfg("x"), task="t", information="")
    assert "ask_kg" not in _read_visible(bb)


def test_visibility_fails_closed_when_enforced_but_scope_missing():
    """Visibility must FAIL CLOSED — not expose the full tool list — when the
    scope contract is enforced but no scope_context is present. (The execution
    layer already raises on this exact invariant; the visibility layer used to
    silently return every tool.)"""
    import pytest
    bb = Blackboard()
    bb.update_state_value("scope_contract_enforced", True)  # no scope_context set
    bb.update_state_value("recently_used_tools", [])
    bb.update_state_value("recently_installed_tools", [])
    with pytest.raises(ValueError, match="unscoped tool list"):
        ToolScopeService().initialize_scope(
            blackboard=bb, tool_registry=_FakeToolRegistry(),
            manager_config=_manager_cfg("emi_team_manager"), task="x", information="")


def test_visibility_fails_closed_when_scope_corrupt():
    """A stored scope that fails validation must fail closed, not fall through
    to the full tool list."""
    import pytest
    bb = Blackboard()
    bb.update_state_value("scope_contract_enforced", True)
    bb.update_state_value("scope_context", {"not": "a valid scope"})  # model_validate raises
    bb.update_state_value("recently_used_tools", [])
    bb.update_state_value("recently_installed_tools", [])
    with pytest.raises(ValueError, match="unscoped tool list"):
        ToolScopeService().initialize_scope(
            blackboard=bb, tool_registry=_FakeToolRegistry(),
            manager_config=_manager_cfg("emi_team_manager"), task="x", information="")


def test_visibility_unrestricted_when_not_enforced():
    """No scope contract requested => visibility unrestricted (by design); must
    NOT raise (the fail-closed guard only fires when enforcement is on)."""
    bb = Blackboard()
    bb.update_state_value("scope_contract_enforced", False)
    bb.update_state_value("recently_used_tools", [])
    bb.update_state_value("recently_installed_tools", [])
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=_manager_cfg("emi_team_manager"), task="x", information="")
    assert _read_visible(bb)  # tools shown, no scope contract to restrict


def test_scope_tool_rule_roundtrip():
    rule = ScopeToolRule(allow=["a", "b"], block=["c"])
    dumped = rule.model_dump()
    back = ScopeToolRule.model_validate(dumped)
    assert back.allow == ["a", "b"]
    assert back.block == ["c"]


def test_scope_tool_policy_per_manager_roundtrip():
    p = ScopeToolPolicy(
        per_manager={
            "emi_team_manager": ScopeToolRule(allow=["web_manager", "pod_search"]),
            "web_manager": ScopeToolRule(block=["http_request"]),
        }
    )
    dumped = p.model_dump()
    back = ScopeToolPolicy.model_validate(dumped)
    assert back.per_manager["emi_team_manager"].allow == ["web_manager", "pod_search"]
    assert back.per_manager["web_manager"].block == ["http_request"]


def test_per_manager_survives_full_scope_context_roundtrip():
    """per_manager passes through ScopeContext model_dump/model_validate —
    i.e. it survives the dict-form storage on the blackboard and any
    inheritance path that round-trips through the validator."""
    p = ScopeToolPolicy(
        per_manager={"emi_team_manager": ScopeToolRule(allow=["pod_search"])}
    )
    sc = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test", tools=p,
    )
    dumped = sc.model_dump()
    back = ScopeContext.model_validate(dumped)
    assert back.tools.per_manager["emi_team_manager"].allow == ["pod_search"]


# ---------------------------------------------------------------------------
# per_manager folds into the narrowed scope (execution + derived visibility)
# ---------------------------------------------------------------------------

def test_per_manager_allow_narrows_visible_tools():
    """When manager X runs and scope.tools.per_manager[X].allow is set, X's
    ceiling collapses to that allow-list — enforced at EXECUTION, and the
    visible set is a subset of it."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            allowed_tools=["all"],
            per_manager={
                "emi_team_manager": ScopeToolRule(
                    allow=["web_manager", "pod_search", "pod_fetch", "ask_user", "find_tool", "install_tool"]
                ),
            },
        ),
    )
    narrowed = _narrow(scope, "emi_team_manager")
    # EXECUTION: the allow-list is the ceiling now.
    assert _can_use(narrowed, "web_manager") is True
    assert _can_use(narrowed, "pod_search") is True
    assert _can_use(narrowed, "personal_admin_manager") is False
    assert _can_use(narrowed, "bash_manager") is False
    assert _can_use(narrowed, "http_request") is False
    # VISIBILITY derives from the narrowed ceiling — a subset of allow.
    bb = _bb_with_scope(narrowed)
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=_manager_cfg("emi_team_manager"), task="search the web", information="",
    )
    visible = _read_visible(bb)
    assert "web_manager" in visible
    assert "pod_search" in visible
    assert "pod_fetch" in visible
    assert "playwright_manager" not in visible
    assert "bash_manager" not in visible
    assert "personal_admin_manager" not in visible
    assert "http_request" not in visible
    assert set(visible) <= set(narrowed.tools.allowed_tools)


def test_per_manager_block_subtracts_visible_tools():
    """per_manager[X].block subtracts from X's surface AND unions into
    blocked_tools, so it binds at execution and propagates to children."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            allowed_tools=["all"],
            per_manager={
                "web_manager": ScopeToolRule(block=["http_request", "oauth_token_refresh"]),
            },
        ),
    )
    narrowed = _narrow(scope, "web_manager")
    # EXECUTION: blocked tools denied; others still reachable.
    assert _can_use(narrowed, "http_request") is False
    assert _can_use(narrowed, "oauth_token_refresh") is False
    assert _can_use(narrowed, "search_web") is True
    # ...and unioned into blocked_tools so the denial propagates down the tree.
    assert "http_request" in narrowed.tools.blocked_tools
    assert "oauth_token_refresh" in narrowed.tools.blocked_tools
    # VISIBILITY mirrors execution.
    bb = _bb_with_scope(narrowed)
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=_manager_cfg("web_manager"), task="search the web", information="",
    )
    visible = _read_visible(bb)
    assert "http_request" not in visible
    assert "oauth_token_refresh" not in visible
    assert "search_web" in visible
    assert "scrape_url" in visible


def test_per_manager_allow_then_block_compose():
    """allow narrows first; block then subtracts from the narrowed set."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            allowed_tools=["all"],
            per_manager={
                "web_manager": ScopeToolRule(
                    allow=["search_web", "scrape_url", "http_request"],
                    block=["http_request"],
                ),
            },
        ),
    )
    narrowed = _narrow(scope, "web_manager")
    assert _can_use(narrowed, "search_web") is True
    assert _can_use(narrowed, "scrape_url") is True
    assert _can_use(narrowed, "http_request") is False    # in allow but also block -> block wins
    bb = _bb_with_scope(narrowed)
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=_manager_cfg("web_manager"), task="search the web", information="",
    )
    visible = _read_visible(bb)
    assert "search_web" in visible
    assert "scrape_url" in visible
    assert "http_request" not in visible


def test_per_manager_unmentioned_manager_is_unchanged():
    """Manager not in per_manager runs with its ceiling unchanged."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            allowed_tools=["all"],
            per_manager={
                "emi_team_manager": ScopeToolRule(allow=["pod_search"]),
            },
        ),
    )
    # Run web_manager — it's not in per_manager, so narrowing is a no-op.
    narrowed = _narrow(scope, "web_manager")
    assert narrowed.tools.allowed_tools == ["all"]
    bb = _bb_with_scope(narrowed)
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=_manager_cfg("web_manager"), task="search the web", information="",
    )
    visible = _read_visible(bb)
    # Should see the full universe (no narrowing happened)
    assert "search_web" in visible
    assert "http_request" in visible  # not blocked because no rule for web_manager
    assert "pod_search" in visible


def test_per_manager_allow_empty_list_blocks_everything():
    """allow=[] is meaningful — it explicitly authorizes nothing. The ceiling
    collapses to []. Distinguishes from allow=None (ceiling unchanged)."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            allowed_tools=["all"],
            per_manager={"emi_team_manager": ScopeToolRule(allow=[])},
        ),
    )
    narrowed = _narrow(scope, "emi_team_manager")
    assert narrowed.tools.allowed_tools == []
    assert _can_use(narrowed, "search_web") is False
    bb = _bb_with_scope(narrowed)
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=_manager_cfg("emi_team_manager"), task="anything", information="",
    )
    visible = _read_visible(bb)
    # Nothing from the tool universe should be visible — allow=[] excludes all.
    assert visible == []


# ---------------------------------------------------------------------------
# Granted sub-manager keeps its OWN surface (starvation fix, 2026-06-13)
# ---------------------------------------------------------------------------

def _narrow_cfg(scope: ScopeContext, manager_name: str, surface: list[str]) -> ScopeContext:
    """Narrow for a manager that declares its own tool surface via config
    tools.allowed_tools (what a real manager config carries)."""
    return ScopeAdapter()._apply_manager_narrowing(
        scope=scope, manager_name=manager_name,
        manager_config={"tools": {"allowed_tools": surface}})


def test_granted_submanager_keeps_own_surface():
    """A sub-manager NAMED in the parent's allow-list runs on its OWN config
    surface — not an intersection with the parent's narrow list. This is the
    starvation fix: sandbox reached via emi_team used to lose execute_code /
    mint_pod because the parent's 16-tool list intersected them away."""
    parent = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="internal",
        tools=ScopeToolPolicy(allowed_tools=["sandbox_manager", "pod_search", "web_manager"]),
        approval=ScopeApprovalPolicy(authority_level=99),
    )
    narrowed = _narrow_cfg(parent, "sandbox_manager", ["execute_code", "pod_search", "pod_fetch"])
    # execute_code / pod_fetch are in the manager's OWN surface but NOT the
    # parent's narrow list — they survive because the manager was granted.
    assert set(narrowed.tools.allowed_tools) == {"execute_code", "pod_search", "pod_fetch"}


def test_ungranted_submanager_bounded_by_ceiling():
    """A manager NOT named in the parent's allow-list is still bounded by the
    inherited ceiling (intersection) — the breach wall holds."""
    parent = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="internal",
        tools=ScopeToolPolicy(allowed_tools=["pod_search"]),  # does NOT name sandbox_manager
        approval=ScopeApprovalPolicy(authority_level=99),
    )
    narrowed = _narrow_cfg(parent, "sandbox_manager", ["execute_code", "pod_search"])
    # not granted -> ceiling wins; execute_code dropped.
    assert set(narrowed.tools.allowed_tools) == {"pod_search"}


def test_empty_ceiling_starves_ungranted_manager():
    """An empty parent allow-list ([]) leaves an ungranted manager with nothing —
    the original Telegram-breach guarantee (a room granting nothing can reach
    nothing)."""
    parent = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="internal",
        tools=ScopeToolPolicy(allowed_tools=[]),
        approval=ScopeApprovalPolicy(authority_level=99),
    )
    narrowed = _narrow_cfg(parent, "sandbox_manager", ["execute_code", "pod_search"])
    assert narrowed.tools.allowed_tools == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
