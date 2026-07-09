"""Room dispatch gates (tool-layer audit T2, 2026-07-09).

execute_dispatch — the room family's dispatch path (chat rooms,
master_room) — used to run tools with NO dispatch-layer checks: the
authority floor and allowlist held only via scope-build filtering + the
agent-layer action validation, and scope.requires_approval_tools /
approval thresholds NEVER fired tickets for room-dispatched calls. The
path now runs the same wall ToolCaller does: check_tool_access (with
the L1 floor) + the approval chain.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.assistant.control_nodes import _tool_caller_util as util
from app.assistant.utils.pydantic_classes import ScopeContext, ToolResult


def _scope(*, authority: int, allowed=("all",), requires_approval=()) -> ScopeContext:
    return ScopeContext.model_validate({
        "scope_id": "scope::test",
        "owner_id": "test",
        "actor_id": "test",
        "surface": "test",
        "tools": {
            "allowed_tools": list(allowed),
            "requires_approval_tools": list(requires_approval),
        },
        "approval": {"authority_level": authority},
    })


class _BB:
    def __init__(self, state=None):
        self._s = dict(state or {})

    def get_state_value(self, key, default=None):
        return self._s.get(key, default)

    def update_state_value(self, key, value):
        self._s[key] = value

    def add_msg(self, msg):
        pass


from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool


class _OkTool(BaseTool):
    def __init__(self):
        super().__init__("test_room_tool")

    def execute(self, tool_message):
        return ToolResult(result_type="success", content="ran", data={"ran": True})


def _tool_config(min_authority=None):
    contract = {"name": "test_room_tool", "description": "t", "inputs": [], "outputs": []}
    contract["metadata"] = {"min_authority": min_authority} if min_authority is not None else {}
    return {"tool_class": _OkTool, "tool_contract": contract}


def _run(scope, tool_config, bb=None):
    return util._execute_tool(
        name="test_caller",
        blackboard=bb or _BB({"scope_contract_enforced": True}),
        tool_registry=SimpleNamespace(),
        tool_name="test_room_tool",
        tool_config=tool_config,
        arguments={},
        scope_context=scope,
    )


def test_authority_floor_blocks_room_dispatch():
    scope = _scope(authority=40)
    with pytest.raises(ValueError, match="requires authority 90"):
        _run(scope, _tool_config(min_authority=90))


def test_scope_allowlist_blocks_room_dispatch():
    scope = _scope(authority=99, allowed=["some_other_tool"])
    with pytest.raises(ValueError, match="outside scope_contract allowed_tools"):
        _run(scope, _tool_config(min_authority=0))


def test_requires_approval_tools_fires_the_gateway(monkeypatch):
    """A user-declared always-ask tool dispatched through a room now goes
    through the approval gateway (it used to execute silently)."""
    calls = []

    def _fake_request(**kwargs):
        calls.append(kwargs)
        blocked = ToolResult(
            result_type="error", content="approval denied",
            data={"error_code": "approval_denied", "abort_policy": "abort_task",
                  "retryable": False, "user_visible": True},
        )
        return False, "ticket-1", blocked

    import app.assistant.lib.tool_execution.tool_approval as ta
    monkeypatch.setattr(ta, "request_approval", _fake_request)

    scope = _scope(authority=90, requires_approval=("test_room_tool",))
    payload = _run(scope, _tool_config(min_authority=0))

    assert len(calls) == 1
    assert calls[0]["approval_reasons"] == ["scope.requires_approval_tools"]
    assert payload["error"] is True                      # denial visible to the planner
    assert "approval denied" in payload["error_message"]


def test_approved_path_executes_and_finalizes(monkeypatch):
    import app.assistant.lib.tool_execution.tool_approval as ta

    finalized = []
    monkeypatch.setattr(ta, "request_approval", lambda **kw: (True, "ticket-2", None))
    monkeypatch.setattr(ta, "finalize_approval_ticket",
                        lambda *, ticket_id, tool_result: finalized.append(ticket_id))

    scope = _scope(authority=90, requires_approval=("test_room_tool",))
    payload = _run(scope, _tool_config(min_authority=0))

    assert payload.get("ran") is True
    assert finalized == ["ticket-2"]


def test_no_approval_reasons_executes_without_gateway(monkeypatch):
    import app.assistant.lib.tool_execution.tool_approval as ta

    def _boom(**kwargs):
        raise AssertionError("gateway must not be called without reasons")

    monkeypatch.setattr(ta, "request_approval", _boom)
    scope = _scope(authority=99)
    payload = _run(scope, _tool_config(min_authority=0))
    assert payload.get("ran") is True
