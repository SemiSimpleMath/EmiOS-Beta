"""Tests for Mailbox: typed message bus per manager invocation."""
from __future__ import annotations

import time

import pytest

from app.assistant.manager_runtime.mailbox import Mailbox, MailboxMessage


@pytest.fixture
def mailbox() -> Mailbox:
    return Mailbox(ttl_seconds=60.0)


@pytest.fixture
def short_ttl_mailbox() -> Mailbox:
    # 50ms TTL — enough to test stale-drop without slowing the suite.
    return Mailbox(ttl_seconds=0.05)


# ── post / drain basics ────────────────────────────────────────────


class TestPostAndDrain:

    def test_post_then_drain_roundtrip(self, mailbox):
        ok = mailbox.post(
            invocation_id="inv-A",
            message_type="agent_inject",
            payload={"planner": "do thing X"},
        )
        assert ok is True

        msgs = mailbox.drain("inv-A")
        assert len(msgs) == 1
        m = msgs[0]
        assert isinstance(m, MailboxMessage)
        assert m.message_type == "agent_inject"
        assert m.payload == {"planner": "do thing X"}

    def test_drain_is_FIFO(self, mailbox):
        for i in range(3):
            mailbox.post(
                invocation_id="inv-B",
                message_type="agent_inject",
                payload={"planner": f"msg {i}"},
            )
        msgs = mailbox.drain("inv-B")
        assert [m.payload["planner"] for m in msgs] == ["msg 0", "msg 1", "msg 2"]

    def test_drain_empties_queue(self, mailbox):
        mailbox.post(
            invocation_id="inv-C",
            message_type="agent_inject",
            payload={"planner": "x"},
        )
        first = mailbox.drain("inv-C")
        second = mailbox.drain("inv-C")
        assert len(first) == 1
        assert second == []

    def test_drain_empty_invocation_returns_empty(self, mailbox):
        assert mailbox.drain("never-posted") == []

    def test_per_invocation_isolation(self, mailbox):
        mailbox.post(invocation_id="A", message_type="agent_inject", payload={"x": "for-A"})
        mailbox.post(invocation_id="B", message_type="agent_inject", payload={"x": "for-B"})
        a = mailbox.drain("A")
        b = mailbox.drain("B")
        assert len(a) == 1 and a[0].payload["x"] == "for-A"
        assert len(b) == 1 and b[0].payload["x"] == "for-B"


# ── input validation ───────────────────────────────────────────────


class TestPostValidation:

    def test_empty_invocation_id_rejected(self, mailbox):
        assert mailbox.post(invocation_id="", message_type="t", payload={"k": "v"}) is False
        assert mailbox.post(invocation_id="   ", message_type="t", payload={"k": "v"}) is False

    def test_empty_message_type_rejected(self, mailbox):
        assert mailbox.post(invocation_id="A", message_type="", payload={"k": "v"}) is False

    def test_non_dict_payload_rejected(self, mailbox):
        assert mailbox.post(invocation_id="A", message_type="t", payload="not a dict") is False  # type: ignore[arg-type]
        assert mailbox.post(invocation_id="A", message_type="t", payload=None) is False  # type: ignore[arg-type]


# ── TTL ────────────────────────────────────────────────────────────


class TestTTL:

    def test_stale_messages_dropped_at_drain(self, short_ttl_mailbox):
        short_ttl_mailbox.post(
            invocation_id="inv-stale",
            message_type="agent_inject",
            payload={"planner": "old"},
        )
        time.sleep(0.1)  # exceed 50ms TTL
        msgs = short_ttl_mailbox.drain("inv-stale")
        assert msgs == []

    def test_fresh_message_survives(self, mailbox):
        mailbox.post(
            invocation_id="inv-fresh",
            message_type="agent_inject",
            payload={"planner": "fresh"},
        )
        msgs = mailbox.drain("inv-fresh")
        assert len(msgs) == 1


# ── peek / clear ───────────────────────────────────────────────────


class TestPeekAndClear:

    def test_peek_count(self, mailbox):
        assert mailbox.peek_count("X") == 0
        mailbox.post(invocation_id="X", message_type="t", payload={"k": "v"})
        mailbox.post(invocation_id="X", message_type="t", payload={"k": "v"})
        assert mailbox.peek_count("X") == 2

    def test_clear(self, mailbox):
        mailbox.post(invocation_id="X", message_type="t", payload={"k": "v"})
        mailbox.clear("X")
        assert mailbox.peek_count("X") == 0
        assert mailbox.drain("X") == []
