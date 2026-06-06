"""Two-level authority: the L1 min_authority floor at execution + visibility.

Phase 2 of the Telegram-breach fix. L1 = ``metadata.min_authority`` is the
see+use floor: ``authority < L1`` -> the tool is invisible AND uncallable,
INDEPENDENT of the allowed_tools ceiling. The breach lacked this wall — a
40-authority guest could reach personal_admin_manager because nothing checked
authority at the access layer (authority only gated whether to PROMPT).

L2 = ``metadata.approval_min_authority`` is the no-approval floor (tested in the
approval suite). Here we pin: every contract carries a valid two-level pair, the
execution gate denies below L1 even with an ["all"] ceiling, and visibility is a
strict subset of what is usable.

Run:
  .venv\\Scripts\\python.exe -m pytest app/assistant/tests/non_agent_tests/test_authority_floor.py
"""
from __future__ import annotations

import json

import pytest

from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.lib.tool_execution.tool_access_control import (
    check_tool_access,
    resolve_tool_min_authority,
)
from app.assistant.manager_runtime.services.tool_scope_service import ToolScopeService
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeToolPolicy,
)

TOOLS_DIR = get_repo_root() / "app" / "assistant" / "lib" / "tools"


def _scope(authority: int, allowed=("all",)) -> ScopeContext:
    return ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="ui", room_id="r",
        approval=ScopeApprovalPolicy(authority_level=authority),
        tools=ScopeToolPolicy(allowed_tools=list(allowed)),
    )


def _can_use(scope: ScopeContext, tool: str, floor: int | None) -> bool:
    ok, _ = check_tool_access(
        tool_name=tool, scope_contract_enforced=True, scope_context=scope,
        task_allowed_tools=None, task_except_tools=None, caller_name="t",
        tool_min_authority=floor)
    return ok


# ── resolve_tool_min_authority ───────────────────────────────────────────────

def test_resolve_floor_explicit():
    assert resolve_tool_min_authority("x", {"tool_contract": {"metadata": {"min_authority": 90}}}) == 90


def test_resolve_floor_contract_present_but_missing_field_fails_closed():
    assert resolve_tool_min_authority("x", {"tool_contract": {"metadata": {}}}) == 99


def test_resolve_floor_no_contract_is_none():
    # No contract (mcp/core/dynamic) -> no floor; the ceiling is the only gate.
    assert resolve_tool_min_authority("mcp::foo", {"tool_class": object}) is None
    assert resolve_tool_min_authority("x", None) is None


def test_resolve_floor_rejects_out_of_range_and_bool():
    with pytest.raises(ValueError):
        resolve_tool_min_authority("x", {"tool_contract": {"metadata": {"min_authority": 101}}})
    with pytest.raises(ValueError):
        resolve_tool_min_authority("x", {"tool_contract": {"metadata": {"min_authority": True}}})


# ── execution gate: the floor denies below L1 even with an ["all"] ceiling ────

def test_floor_denies_below_even_with_all_ceiling():
    # THE breach wall: allow=[all] but authority 40 < floor 90 -> DENIED.
    assert _can_use(_scope(40, ["all"]), "personal_admin_manager", 90) is False


def test_floor_allows_at_or_above():
    assert _can_use(_scope(90, ["all"]), "personal_admin_manager", 90) is True


def test_floor_admin_100_clears_every_floor():
    assert _can_use(_scope(100, ["all"]), "bash_manager", 99) is True


def test_floor_none_means_no_floor():
    assert _can_use(_scope(0, ["all"]), "mcp::whatever", None) is True


# ── data integrity over ALL contracts (the fail-closed CI validator) ─────────

def _all_contracts() -> dict:
    return {p.parent.name: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(TOOLS_DIR.glob("*/tool_contract.json"))}


def test_every_contract_has_valid_two_level_authority():
    bad = []
    for name, c in _all_contracts().items():
        md = c.get("metadata") if isinstance(c.get("metadata"), dict) else {}
        l1, l2 = md.get("min_authority"), md.get("approval_min_authority")
        if not isinstance(l1, int) or isinstance(l1, bool) or not (0 <= l1 <= 100):
            bad.append((name, "L1", l1))
        elif not isinstance(l2, int) or isinstance(l2, bool) or not (0 <= l2 <= 100):
            bad.append((name, "L2", l2))
        elif l1 > l2:
            bad.append((name, "L1>L2", (l1, l2)))
    assert not bad, f"contracts with invalid two-level authority: {bad}"


# L2 defaults to L1 (no approval band). A band (L2 > L1) exists ONLY where a tool
# deliberately demands confirm-even-when-authorized: send_email + calendar deletion.
@pytest.mark.parametrize("name,l1,l2", [
    ("personal_admin_manager", 90, 90),
    ("send_email", 90, 99),               # deliberate band; softens to 95 via allowlist
    ("delete_calendar_event", 90, 95),    # deliberate band
    ("web_manager", 30, 30),              # no band — clears floor => uses freely
    ("emi_team_manager", 30, 30),
    ("http_request", 70, 70),
    ("bash_manager", 99, 99),
    ("sandbox_manager", 99, 99),
    ("get_weather", 10, 10),
    ("ask_user", 0, 0),
    ("pod_search", 0, 0),                 # no floor — minting room reads its own pods
    ("pod_fetch", 0, 0),
])
def test_anchor_contract_values(name, l1, l2):
    md = json.loads((TOOLS_DIR / name / "tool_contract.json").read_text(encoding="utf-8"))["metadata"]
    assert md["min_authority"] == l1, f"{name} L1"
    assert md["approval_min_authority"] == l2, f"{name} L2"


# ── visibility floor: a subset of what is usable ─────────────────────────────

_UNIVERSE = {
    "web_manager": {"tool_contract": {"metadata": {"min_authority": 30}}},
    "personal_admin_manager": {"tool_contract": {"metadata": {"min_authority": 90}}},
    "bash_manager": {"tool_contract": {"metadata": {"min_authority": 99}}},
    "get_weather": {"tool_contract": {"metadata": {"min_authority": 10}}},
    "ask_user": {"tool_contract": {"metadata": {"min_authority": 0}}},
}


class _FakeReg:
    def get_all_tools(self): return dict(_UNIVERSE)
    def get_tool_descriptions(self, names): return {n: n for n in names}
    def get_tool(self, n): return _UNIVERSE.get(n)


def _visible(authority: int) -> set:
    bb = Blackboard()
    bb.update_state_value("scope_context", _scope(authority, ["all"]).model_dump())
    bb.update_state_value("scope_contract_enforced", True)
    bb.update_state_value("recently_used_tools", [])
    bb.update_state_value("recently_installed_tools", [])
    ToolScopeService().initialize_scope(
        blackboard=bb, tool_registry=_FakeReg(),
        manager_config={"name": "m", "tool_visibility": {"use_narrower": False}},
        task="do something", information="")
    raw = bb.get_state_value("visible_tools")
    return set(raw) if isinstance(raw, list) else set()


def test_visibility_floor_hides_above_authority():
    vis = _visible(40)
    assert {"web_manager", "get_weather", "ask_user"} <= vis
    assert "personal_admin_manager" not in vis   # floor 90 > 40
    assert "bash_manager" not in vis             # floor 99 > 40


def test_visibility_is_subset_of_usable():
    authority = 40
    for t in _visible(authority):
        assert _can_use(_scope(authority, ["all"]), t, resolve_tool_min_authority(t, _UNIVERSE.get(t))), \
            f"{t} visible but not usable at authority {authority}"


def test_visibility_full_at_admin_authority():
    assert _visible(100) >= {"web_manager", "personal_admin_manager", "bash_manager", "get_weather", "ask_user"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
