"""Tests for ScopePodPolicy.allowed_scopes — cross-room pod visibility.

Pods are tagged with scope_id (typically room_id where minted). This dimension
declares which scope_ids the calling room can read. Sensitivity gating
(min_authority) is SEPARATE and not exercised by these tests.

  - ["all"]: owner-wide visibility (master_room)
  - ["self"]: own-room only (slack channels — DEFAULT)
  - explicit list: that exact set (+ self if listed)
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.assistant.utils.pydantic_classes import (
    ScopeContext,
    ScopePodPolicy,
    ToolMessage,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_pod_policy_default_is_self():
    p = ScopePodPolicy()
    assert p.allowed_scopes == ["self"]


def test_pod_policy_all_keeps_just_all():
    p = ScopePodPolicy(allowed_scopes=["all"])
    assert p.allowed_scopes == ["all"]


def test_pod_policy_rejects_all_mixed_with_specifics():
    import pytest
    with pytest.raises(Exception):
        ScopePodPolicy(allowed_scopes=["all", "slack/A"])


def test_pod_policy_empty_falls_back_to_self():
    p = ScopePodPolicy(allowed_scopes=[])
    assert p.allowed_scopes == ["self"]


def test_pod_policy_explicit_list_preserved():
    p = ScopePodPolicy(allowed_scopes=["self", "slack/B", "telegram/X"])
    assert p.allowed_scopes == ["self", "slack/B", "telegram/X"]


def test_pod_policy_roundtrip_via_scope_context():
    sc = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack/test",
        pods=ScopePodPolicy(allowed_scopes=["all"]),
    )
    back = ScopeContext.model_validate(sc.model_dump())
    assert back.pods.allowed_scopes == ["all"]


# ---------------------------------------------------------------------------
# _resolve_pod_allowed_scopes helper
# ---------------------------------------------------------------------------

def _tm_with_scope(scope_ctx) -> ToolMessage:
    tm = ToolMessage(tool_name="pod_search", tool_data={})
    tm.scope_context = scope_ctx
    return tm


def test_resolve_master_room_all_returns_all():
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import _resolve_pod_allowed_scopes
    sc = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="ui",
        room_id="master_room",
        pods=ScopePodPolicy(allowed_scopes=["all"]),
    )
    assert _resolve_pod_allowed_scopes(_tm_with_scope(sc)) == ["all"]


def test_resolve_self_expands_to_room_id():
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import _resolve_pod_allowed_scopes
    sc = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack/__test__",
        pods=ScopePodPolicy(),  # default = ["self"]
    )
    assert _resolve_pod_allowed_scopes(_tm_with_scope(sc)) == ["slack/__test__"]


def test_resolve_mixed_self_and_explicit_concatenates():
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import _resolve_pod_allowed_scopes
    sc = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="slack",
        room_id="slack/A",
        pods=ScopePodPolicy(allowed_scopes=["self", "slack/B"]),
    )
    assert _resolve_pod_allowed_scopes(_tm_with_scope(sc)) == ["slack/A", "slack/B"]


def test_resolve_dict_form_scope_context():
    """Blackboard stores scope_context as a dict (model_dump'd) — the helper
    must handle both that and the ScopeContext object form."""
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import _resolve_pod_allowed_scopes
    sc = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="ui",
        room_id="master_room",
        pods=ScopePodPolicy(allowed_scopes=["all"]),
    )
    tm = ToolMessage(tool_name="pod_search", tool_data={})
    tm.scope_context = sc.model_dump()
    assert _resolve_pod_allowed_scopes(tm) == ["all"]


def test_resolve_no_scope_context_falls_back_to_all():
    """System-internal callers (routines, dayflow) without a scope_context
    should NOT be restricted — preserve pre-existing unrestricted behavior."""
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import _resolve_pod_allowed_scopes
    tm = ToolMessage(tool_name="pod_search", tool_data={})
    assert _resolve_pod_allowed_scopes(tm) == ["all"]


def test_resolve_self_without_room_id_returns_sentinel():
    """If scope says 'self' but has no room_id, there's nothing to bind to —
    helper returns a sentinel that the query path treats as 'no readable scope'."""
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import _resolve_pod_allowed_scopes
    sc = ScopeContext(
        scope_id="s", owner_id="u", actor_id="a", surface="system",
        room_id=None,
        pods=ScopePodPolicy(),  # default ["self"]
    )
    result = _resolve_pod_allowed_scopes(_tm_with_scope(sc))
    assert result == ["__none__"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
