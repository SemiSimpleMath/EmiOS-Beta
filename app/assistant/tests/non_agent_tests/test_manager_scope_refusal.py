"""MultiAgentManager refuses to run ungated in production (scope audit #2,
2026-06-13).

A Message with no scope_context used to set scope_contract_enforced=False —
the manager ran with the scope/authority gate fully OFF. Now: production
RAISES (fail-closed), and test mode substitutes a permissive scope so harnesses
that don't build their own scope still run.
"""
from __future__ import annotations

import pytest

from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager


class _FakeBlackboard:
    def __init__(self):
        self.state = {}

    def update_state_value(self, k, v):
        self.state[k] = v


def _manager() -> MultiAgentManager:
    m = object.__new__(MultiAgentManager)  # bypass __init__ (no DI needed)
    m.name = "test_manager"
    m.blackboard = _FakeBlackboard()
    return m


def test_no_scope_refuses_in_production(monkeypatch):
    monkeypatch.delenv("EMI_TEST_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    m = _manager()
    with pytest.raises(ValueError, match="refusing to run ungated"):
        m._apply_no_inbound_scope()
    # Nothing was written — it failed loud, didn't silently set enforced=False.
    assert "scope_contract_enforced" not in m.blackboard.state


def test_no_scope_substitutes_permissive_scope_in_test_mode(monkeypatch):
    monkeypatch.setenv("EMI_TEST_MODE", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    m = _manager()
    m._apply_no_inbound_scope()
    assert m.blackboard.state["scope_contract_enforced"] is True
    sc = m.blackboard.state["scope_context"]
    assert sc["tools"]["allowed_tools"] == ["all"]
    assert sc["approval"]["authority_level"] == 100


def test_pytest_env_alone_counts_as_test_mode(monkeypatch):
    monkeypatch.delenv("EMI_TEST_MODE", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "some::test")
    m = _manager()
    m._apply_no_inbound_scope()  # must not raise
    assert m.blackboard.state["scope_contract_enforced"] is True
