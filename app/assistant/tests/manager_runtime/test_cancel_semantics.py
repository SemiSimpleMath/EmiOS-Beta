"""Cancel semantics for the manager runtime.

Pins the two halves of the 2026-07-08 runtime-audit cancel fix:
1. A cancelled run returns an aborted ToolResult — every exit-reason branch
   returns a ToolResult (the old handle_graceful_exit_reason had no
   "cancelled" branch and returned None to callers typed for ToolResult).
2. MAMInstanceManager.cancel writes the flag into the blackboard's GLOBAL
   scope, so a nested agent-call scope popping cannot discard it.
"""
from __future__ import annotations

from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager
from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
from app.assistant.utils.pydantic_classes import ToolResult


def _bare_manager(name: str = "test_manager") -> MultiAgentManager:
    """A MultiAgentManager shell with just the attributes the exit paths read
    (constructing a real one needs agent/tool registries)."""
    mgr = MultiAgentManager.__new__(MultiAgentManager)
    mgr.name = name
    mgr.blackboard = Blackboard()
    mgr.flow_config = {}
    mgr.manager_config = {}
    return mgr


class TestCancelledExitReason:

    def test_handle_exit_reason_cancelled_returns_aborted_toolresult(self):
        mgr = _bare_manager()
        result = mgr.handle_exit_reason("cancelled")
        assert isinstance(result, ToolResult)
        assert result.result_type == "manager_aborted"
        assert "cancel" in result.content.lower()
        assert result.data.get("exit_state") == "cancelled"

    def test_handle_graceful_exit_reason_cancelled_returns_toolresult(self):
        mgr = _bare_manager()
        result = mgr.handle_graceful_exit_reason("cancelled")
        assert isinstance(result, ToolResult)
        assert result.result_type == "manager_aborted"

    def test_handle_graceful_exit_reason_never_returns_none(self):
        mgr = _bare_manager()
        for reason in ("cancelled", "max_cycles", "error", "something_unexpected"):
            result = mgr.handle_graceful_exit_reason(reason)
            assert isinstance(result, ToolResult), f"reason={reason!r} returned {result!r}"


class TestCancelWritesGlobalScope:

    def test_cancel_survives_nested_scope_pop(self):
        mam = MAMInstanceManager()

        class _FakeManager:
            def __init__(self):
                self.blackboard = Blackboard()

        manager = _FakeManager()
        record = mam.register(
            manager_instance=manager,
            manager_instance_name="fake_manager_abc12345",
            manager_name="fake_manager",
            base_display_name="Fakey",
            request_id=None,
            room_id=None,
            reply_to=None,
        )

        # Simulate the manager mid nested agent call: a pushed call scope is
        # the blackboard's top scope when the cancel arrives from another thread.
        manager.blackboard.push_call_context("planner", "helper_agent", "scope_nested")
        assert mam.cancel(record.invocation_id) is True

        # The nested call finishes and its scope pops — the flag must survive.
        manager.blackboard.pop_call_context()
        assert manager.blackboard.get_state_value("cancelled", False) is True

        mam.unregister(record.invocation_id)

    def test_cancel_unknown_invocation_returns_false(self):
        mam = MAMInstanceManager()
        assert mam.cancel("no-such-invocation") is False
