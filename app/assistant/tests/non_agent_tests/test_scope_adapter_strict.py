"""Manager ingress refuses a scope-less request by default (scope audit #3,
2026-06-13, owner directive "ingress should refuse too").

scope_adapter used to DERIVE a wide system scope (allow-all, authority 0) for
any Message that reached manager ingress with no inbound scope, no room, and no
data scope-contract. That silently re-opened the gate one level above the
manager. Now strict mode is the DEFAULT in production: such a request RAISES.

The allowlist (_allow_strict_mode_system_derivation) still lets genuine
task-scoped / resource-scoped / scope-seeded requests derive a system scope, so
the maintenance + task-runner paths that legitimately carry those signals keep
working. Test mode stays lenient (derive, not refuse) so harnesses that invoke
via the invoker without building a scope still run.
"""
from __future__ import annotations

import pytest

import app.assistant.tests.test_setup  # noqa: F401  (bootstraps DI + EMI_TEST_MODE)
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
from app.assistant.utils.pydantic_classes import Message


# --- the flip: _strict_scope_enabled default ------------------------------

def test_strict_default_on_in_production(monkeypatch):
    monkeypatch.delenv("SCOPE_CONTRACT_STRICT", raising=False)
    monkeypatch.delenv("EMI_TEST_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert ScopeAdapter._strict_scope_enabled() is True


def test_strict_lenient_in_test_mode(monkeypatch):
    monkeypatch.delenv("SCOPE_CONTRACT_STRICT", raising=False)
    monkeypatch.setenv("EMI_TEST_MODE", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert ScopeAdapter._strict_scope_enabled() is False


def test_strict_lenient_under_pytest_marker_alone(monkeypatch):
    monkeypatch.delenv("SCOPE_CONTRACT_STRICT", raising=False)
    monkeypatch.delenv("EMI_TEST_MODE", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "some::test")
    assert ScopeAdapter._strict_scope_enabled() is False


def test_explicit_env_override_wins(monkeypatch):
    # An explicit setting beats the test-mode default in both directions.
    monkeypatch.setenv("EMI_TEST_MODE", "1")
    monkeypatch.setenv("SCOPE_CONTRACT_STRICT", "true")
    assert ScopeAdapter._strict_scope_enabled() is True
    monkeypatch.delenv("EMI_TEST_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SCOPE_CONTRACT_STRICT", "false")
    assert ScopeAdapter._strict_scope_enabled() is False


# --- the allowlist: which scope-less requests may still derive -------------

def test_allowlist_permits_task_scoped_request():
    msg = Message(task="x", data={"task_file": "tasks/x/task_spec.md"})
    assert ScopeAdapter._allow_strict_mode_system_derivation(message=msg) is True


def test_allowlist_permits_scope_seeded_request():
    msg = Message(task="x", data={"scope_contract": {"scope_id": "s"}})
    assert ScopeAdapter._allow_strict_mode_system_derivation(message=msg) is True


def test_allowlist_permits_resource_scoped_request():
    msg = Message(task="x", data={"allowed_global_resources": ["resource_x"]})
    assert ScopeAdapter._allow_strict_mode_system_derivation(message=msg) is True


def test_allowlist_rejects_signal_less_request():
    msg = Message(task="x")
    assert ScopeAdapter._allow_strict_mode_system_derivation(message=msg) is False


# --- end-to-end: the refuse fires at ingress ------------------------------

def test_resolve_refuses_signal_less_request_under_strict(monkeypatch):
    # Force strict regardless of test mode (explicit env wins) to exercise the
    # production refuse path in-process.
    monkeypatch.setenv("SCOPE_CONTRACT_STRICT", "true")
    adapter = object.__new__(ScopeAdapter)  # bypass __init__: refuse path is self-contained
    msg = Message(task="x")  # no scope, no room, no data scope-contract, no allowlist signal
    with pytest.raises(ValueError, match="Missing scope_context at manager ingress"):
        adapter._resolve_scope_context(manager_name="m", manager_config={}, message=msg)
