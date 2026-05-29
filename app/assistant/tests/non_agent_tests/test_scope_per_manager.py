"""Tests for ScopeToolPolicy.per_manager — per-manager allow/block rules.

These rules fire when a specific manager runs in the scope's call tree:
- rule.allow (when not None) REPLACES the manager's effective surface with
  the intersection of the ranked list and rule.allow.
- rule.block subtracts specific items from the resulting surface.

Unmentioned managers run with their natives unchanged.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.manager_runtime.services.tool_scope_service import ToolScopeService
from app.assistant.utils.pydantic_classes import (
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


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------

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
# tool_scope_service enforcement
# ---------------------------------------------------------------------------

def test_per_manager_allow_narrows_visible_tools():
    """When manager X runs and scope.tools.per_manager[X].allow is set,
    X's planner only sees the intersection of ranked and allow."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            per_manager={
                "emi_team_manager": ScopeToolRule(
                    allow=["web_manager", "pod_search", "pod_fetch", "ask_user", "find_tool", "install_tool"]
                ),
            }
        ),
    )
    bb = _bb_with_scope(scope)
    cfg = _manager_cfg("emi_team_manager")
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=cfg, task="search the web", information="",
    )
    visible = _read_visible(bb)
    # Everything in allow should be present; nothing outside.
    assert "web_manager" in visible
    assert "pod_search" in visible
    assert "pod_fetch" in visible
    # NOT in allow → must be filtered out
    assert "playwright_manager" not in visible
    assert "bash_manager" not in visible
    assert "personal_admin_manager" not in visible
    assert "http_request" not in visible


def test_per_manager_block_subtracts_visible_tools():
    """When manager X runs and scope.tools.per_manager[X].block is set,
    those specific tools are removed from X's surface."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            per_manager={
                "web_manager": ScopeToolRule(block=["http_request", "oauth_token_refresh"]),
            }
        ),
    )
    bb = _bb_with_scope(scope)
    cfg = _manager_cfg("web_manager")
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=cfg, task="search the web", information="",
    )
    visible = _read_visible(bb)
    # Blocked items must be gone
    assert "http_request" not in visible
    assert "oauth_token_refresh" not in visible
    # Other web_manager tools survive
    assert "search_web" in visible
    assert "scrape_url" in visible


def test_per_manager_allow_then_block_compose():
    """allow narrows first; block then subtracts from the narrowed set."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            per_manager={
                "web_manager": ScopeToolRule(
                    allow=["search_web", "scrape_url", "http_request"],
                    block=["http_request"],
                ),
            }
        ),
    )
    bb = _bb_with_scope(scope)
    cfg = _manager_cfg("web_manager")
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=cfg, task="search the web", information="",
    )
    visible = _read_visible(bb)
    assert "search_web" in visible
    assert "scrape_url" in visible
    # In allow but also in block → blocked wins
    assert "http_request" not in visible


def test_per_manager_unmentioned_manager_is_unchanged():
    """Manager not in per_manager runs with natives unchanged."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            per_manager={
                "emi_team_manager": ScopeToolRule(allow=["pod_search"]),
            }
        ),
    )
    bb = _bb_with_scope(scope)
    # Run web_manager — it's not in per_manager, so no narrowing applies.
    cfg = _manager_cfg("web_manager")
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=cfg, task="search the web", information="",
    )
    visible = _read_visible(bb)
    # Should see the full universe (no narrowing happened)
    assert "search_web" in visible
    assert "http_request" in visible  # not blocked because no rule for web_manager
    assert "pod_search" in visible


def test_per_manager_allow_empty_list_blocks_everything():
    """allow=[] is meaningful — it explicitly authorizes nothing.
    The manager's planner would see only intrinsic operational tools.
    Distinguishes from allow=None (unrestricted natives)."""
    scope = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack:test",
        tools=ScopeToolPolicy(
            per_manager={"emi_team_manager": ScopeToolRule(allow=[])}
        ),
    )
    bb = _bb_with_scope(scope)
    cfg = _manager_cfg("emi_team_manager")
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeToolRegistry(),
        manager_config=cfg, task="anything", information="",
    )
    visible = _read_visible(bb)
    # Nothing from the tool universe should be visible — allow=[] excludes all.
    assert visible == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
