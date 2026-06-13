"""enforce_scope_contract must fail loud, not swallow (scope audit #5, 2026-06-13).

It raises when scope_contract_enforced=true but no scope_context is set. The
flag/scope reads used to be wrapped in try/except that defaulted enforced=False
on a read error — a fail-open swallow (a broken blackboard would skip the gate).
Removed: a read failure now propagates; only the legitimate 'flag absent => not
enforced' default remains.
"""
from __future__ import annotations

import pytest

from app.assistant.agent_runtime.services.agent_input_applier import AgentInputApplier
from app.assistant.lib.blackboard.Blackboard import Blackboard


def _applier(state: dict) -> AgentInputApplier:
    bb = Blackboard()
    for k, v in state.items():
        bb.update_state_value(k, v)
    return AgentInputApplier("test_agent", bb)


def test_enforced_without_scope_raises():
    applier = _applier({"scope_contract_enforced": True})  # no scope_context
    with pytest.raises(ValueError, match="Missing scope_context"):
        applier.enforce_scope_contract()


def test_enforced_with_scope_present_ok():
    applier = _applier({"scope_contract_enforced": True, "scope_context": {"scope_id": "s"}})
    applier.enforce_scope_contract()  # presence is enough; must not raise


def test_not_enforced_is_noop():
    applier = _applier({"scope_contract_enforced": False})  # no contract requested
    applier.enforce_scope_contract()  # must not raise


def test_flag_absent_is_noop():
    applier = _applier({})  # flag never set => not enforced (legitimate default)
    applier.enforce_scope_contract()  # must not raise


def test_read_failure_propagates_not_swallowed():
    """A blackboard read failure must fail loud, not default enforced=False."""
    class _BoomBlackboard:
        def get_state_value(self, *a, **k):
            raise RuntimeError("blackboard down")

    applier = AgentInputApplier("test_agent", _BoomBlackboard())
    with pytest.raises(RuntimeError, match="blackboard down"):
        applier.enforce_scope_contract()
