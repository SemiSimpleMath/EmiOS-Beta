"""Tests for MultiAgentManager._drain_mailbox + _dispatch_mailbox_message.

Validates the manager-side of the typed-message bus: drain → dispatch by
message_type → either accumulate per-agent runtime injection or write to
blackboard.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.assistant.manager_runtime.mailbox import MailboxMessage


class _FakeBlackboard:
    def __init__(self):
        self._state: dict = {}

    def get_state_value(self, key, default=None):
        return self._state.get(key, default)

    def update_state_value(self, key, value):
        self._state[key] = value

    def update_global_state_value(self, key, value):
        self._state[key] = value


class _FakeManager:
    """Minimal stand-in for MultiAgentManager — just the methods we test."""

    _RUNTIME_INJECTIONS_BB_KEY = "_runtime_injections"

    def __init__(self):
        self.name = "fake_manager"
        self.blackboard = _FakeBlackboard()
        self._role_bindings = {"planner": "fake::planner"}

    def resolve_role_binding(self, role_name):
        return self._role_bindings.get(role_name, role_name)


def _make_msg(*, mtype, payload):
    return MailboxMessage(
        message_type=mtype,
        payload=payload,
        posted_at_utc=datetime.now(timezone.utc),
    )


# We attach the real MultiAgentManager methods to the FakeManager so we
# don't need a full DI bootstrap to test the dispatch logic in isolation.
@pytest.fixture
def manager():
    from app.assistant.manager_classes.MultiAgentManager import MultiAgentManager
    m = _FakeManager()
    m._dispatch_mailbox_message = MultiAgentManager._dispatch_mailbox_message.__get__(m)
    m._append_runtime_injection = MultiAgentManager._append_runtime_injection.__get__(m)
    return m


# ── agent_inject: appends per agent name ──────────────────────────


class TestAgentInject:

    def test_role_resolved_via_binding(self, manager):
        manager._dispatch_mailbox_message(_make_msg(
            mtype="agent_inject",
            payload={"planner": "do thing X"},
        ))
        store = manager.blackboard.get_state_value(manager._RUNTIME_INJECTIONS_BB_KEY)
        # "planner" → "fake::planner" via role bindings.
        assert store == {"fake::planner": ["do thing X"]}

    def test_unknown_role_passes_through_as_literal(self, manager):
        manager._dispatch_mailbox_message(_make_msg(
            mtype="agent_inject",
            payload={"web::planner": "literal name"},
        ))
        store = manager.blackboard.get_state_value(manager._RUNTIME_INJECTIONS_BB_KEY)
        assert store == {"web::planner": ["literal name"]}

    def test_multiple_messages_accumulate(self, manager):
        manager._dispatch_mailbox_message(_make_msg(
            mtype="agent_inject", payload={"planner": "first"},
        ))
        manager._dispatch_mailbox_message(_make_msg(
            mtype="agent_inject", payload={"planner": "second"},
        ))
        store = manager.blackboard.get_state_value(manager._RUNTIME_INJECTIONS_BB_KEY)
        assert store == {"fake::planner": ["first", "second"]}

    def test_empty_content_skipped(self, manager):
        manager._dispatch_mailbox_message(_make_msg(
            mtype="agent_inject", payload={"planner": "   "},
        ))
        store = manager.blackboard.get_state_value(manager._RUNTIME_INJECTIONS_BB_KEY)
        # Nothing was written.
        assert store is None

    def test_per_agent_isolation(self, manager):
        manager._dispatch_mailbox_message(_make_msg(
            mtype="agent_inject", payload={"planner": "for planner"},
        ))
        manager._dispatch_mailbox_message(_make_msg(
            mtype="agent_inject", payload={"reviewer": "for reviewer"},
        ))
        store = manager.blackboard.get_state_value(manager._RUNTIME_INJECTIONS_BB_KEY)
        assert store == {
            "fake::planner": ["for planner"],
            "reviewer": ["for reviewer"],
        }


# ── blackboard_write: simple set ──────────────────────────────────


class TestBlackboardWrite:

    def test_writes_each_key(self, manager):
        manager._dispatch_mailbox_message(_make_msg(
            mtype="blackboard_write",
            payload={"cancelled": True, "extra_context": "from outside"},
        ))
        assert manager.blackboard.get_state_value("cancelled") is True
        assert manager.blackboard.get_state_value("extra_context") == "from outside"

    def test_empty_key_skipped(self, manager):
        manager._dispatch_mailbox_message(_make_msg(
            mtype="blackboard_write",
            payload={"": "lost", "real": "kept"},
        ))
        assert manager.blackboard.get_state_value("real") == "kept"


# ── unknown type: logged + dropped, no crash ──────────────────────


class TestUnknownType:

    def test_unknown_type_does_not_raise(self, manager):
        # Should not raise; should not write anything.
        manager._dispatch_mailbox_message(_make_msg(
            mtype="some_future_type", payload={"k": "v"},
        ))
        assert manager.blackboard.get_state_value("k") is None
