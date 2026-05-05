"""Tests for MailboxDispatcher — drain + dispatch logic for the typed
message bus into a running manager.

Validates the dispatcher in isolation: each ``message_type`` lands on
the right blackboard slot, role bindings are honored, unknown types are
dropped without crashing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.assistant.manager_runtime.mailbox import (
    MailboxDispatcher,
    MailboxMessage,
    _RUNTIME_INJECTIONS_BB_KEY,
)


class _FakeBlackboard:
    def __init__(self):
        self._state: dict = {}

    def get_state_value(self, key, default=None):
        return self._state.get(key, default)

    def update_state_value(self, key, value):
        self._state[key] = value


def _make_msg(*, mtype, payload):
    return MailboxMessage(
        message_type=mtype,
        payload=payload,
        posted_at_utc=datetime.now(timezone.utc),
    )


@pytest.fixture
def dispatcher():
    return MailboxDispatcher()


@pytest.fixture
def role_resolver():
    return {"planner": "fake::planner"}.get


# ── agent_inject: appends per agent name ─────────────────────────


class TestAgentInject:

    def test_role_resolved_via_resolver(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"planner": "do thing X"}),
            bb, role_resolver,
        )
        assert bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) == {
            "fake::planner": ["do thing X"],
        }

    def test_unknown_role_passes_through_as_literal(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"web::planner": "literal name"}),
            bb, role_resolver,
        )
        assert bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) == {
            "web::planner": ["literal name"],
        }

    def test_multiple_messages_accumulate(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"planner": "first"}),
            bb, role_resolver,
        )
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"planner": "second"}),
            bb, role_resolver,
        )
        assert bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) == {
            "fake::planner": ["first", "second"],
        }

    def test_empty_content_skipped(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"planner": "   "}),
            bb, role_resolver,
        )
        assert bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) is None

    def test_per_agent_isolation(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"planner": "for planner"}),
            bb, role_resolver,
        )
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"reviewer": "for reviewer"}),
            bb, role_resolver,
        )
        assert bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) == {
            "fake::planner": ["for planner"],
            "reviewer": ["for reviewer"],
        }

    def test_no_resolver_falls_back_to_literal_role(self, dispatcher):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="agent_inject", payload={"planner": "x"}),
            bb, role_resolver=None,
        )
        # No resolver → role string used as agent name verbatim.
        assert bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) == {"planner": ["x"]}


# ── blackboard_write: simple set ─────────────────────────────────


class TestBlackboardWrite:

    def test_writes_each_key(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="blackboard_write",
                      payload={"cancelled": True, "extra_context": "from outside"}),
            bb, role_resolver,
        )
        assert bb.get_state_value("cancelled") is True
        assert bb.get_state_value("extra_context") == "from outside"

    def test_empty_key_skipped(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        dispatcher._dispatch_one(
            _make_msg(mtype="blackboard_write", payload={"": "lost", "real": "kept"}),
            bb, role_resolver,
        )
        assert bb.get_state_value("real") == "kept"


# ── unknown type: logged + dropped, no crash ─────────────────────


class TestUnknownType:

    def test_unknown_type_does_not_raise(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        # Should not raise; should not write anything.
        dispatcher._dispatch_one(
            _make_msg(mtype="some_future_type", payload={"k": "v"}),
            bb, role_resolver,
        )
        assert bb.get_state_value("k") is None


# ── drain_to: end-to-end with a mocked Mailbox ──────────────────


class TestDrainTo:

    def test_drain_to_applies_all_messages(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        bb.update_state_value("_invocation_id", "inv-A")

        mock_mailbox = MagicMock()
        mock_mailbox.drain.return_value = [
            _make_msg(mtype="agent_inject", payload={"planner": "first"}),
            _make_msg(mtype="blackboard_write", payload={"cancelled": True}),
        ]
        with patch.object(dispatcher, "_resolve_mailbox", return_value=mock_mailbox):
            applied = dispatcher.drain_to(
                blackboard=bb,
                invocation_id="inv-A",
                role_resolver=role_resolver,
            )

        assert applied == 2
        assert bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) == {
            "fake::planner": ["first"],
        }
        assert bb.get_state_value("cancelled") is True
        mock_mailbox.drain.assert_called_once_with("inv-A")

    def test_empty_invocation_id_returns_zero(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        applied = dispatcher.drain_to(
            blackboard=bb, invocation_id="", role_resolver=role_resolver,
        )
        assert applied == 0

    def test_no_mailbox_returns_zero(self, dispatcher, role_resolver):
        bb = _FakeBlackboard()
        with patch.object(dispatcher, "_resolve_mailbox", return_value=None):
            applied = dispatcher.drain_to(
                blackboard=bb, invocation_id="inv-A", role_resolver=role_resolver,
            )
        assert applied == 0
