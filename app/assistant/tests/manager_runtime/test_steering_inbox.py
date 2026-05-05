"""Tests for SteeringInbox: post / pop / TTL / dedup behavior."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.manager_runtime.steering_inbox import (
    SteeringInbox,
    SteeringMessage,
)


@pytest.fixture
def inbox():
    return SteeringInbox(ttl_seconds=60.0)


@pytest.fixture
def short_ttl_inbox():
    """TTL just barely larger than zero so we can test stale-drop without sleeping."""
    return SteeringInbox(ttl_seconds=0.05)


class TestPostAndPop:

    def test_post_then_pop_returns_message(self, inbox):
        assert inbox.post(invocation_id="inv1", text="check savory") is True
        msgs = inbox.pop_all("inv1")
        assert len(msgs) == 1
        assert msgs[0].text == "check savory"
        assert isinstance(msgs[0], SteeringMessage)

    def test_pop_drains_inbox(self, inbox):
        inbox.post(invocation_id="inv1", text="m1")
        inbox.post(invocation_id="inv1", text="m2")
        first = inbox.pop_all("inv1")
        second = inbox.pop_all("inv1")
        assert [m.text for m in first] == ["m1", "m2"]
        assert second == []

    def test_pop_preserves_order(self, inbox):
        for n in range(5):
            inbox.post(invocation_id="inv1", text=f"m{n}")
        msgs = inbox.pop_all("inv1")
        assert [m.text for m in msgs] == ["m0", "m1", "m2", "m3", "m4"]

    def test_inbox_isolated_per_invocation(self, inbox):
        inbox.post(invocation_id="A", text="for_A")
        inbox.post(invocation_id="B", text="for_B")
        assert [m.text for m in inbox.pop_all("A")] == ["for_A"]
        assert [m.text for m in inbox.pop_all("B")] == ["for_B"]


class TestRejectInputs:

    def test_empty_invocation_returns_false(self, inbox):
        assert inbox.post(invocation_id="", text="x") is False
        assert inbox.post(invocation_id=None, text="x") is False  # type: ignore[arg-type]

    def test_empty_text_returns_false(self, inbox):
        assert inbox.post(invocation_id="inv1", text="") is False
        assert inbox.post(invocation_id="inv1", text="   ") is False
        assert inbox.post(invocation_id="inv1", text=None) is False  # type: ignore[arg-type]

    def test_pop_unknown_invocation_returns_empty(self, inbox):
        assert inbox.pop_all("nonexistent") == []


class TestTTL:

    def test_stale_message_is_dropped_at_pop(self, short_ttl_inbox):
        short_ttl_inbox.post(invocation_id="inv1", text="will go stale")
        time.sleep(0.1)  # exceed 0.05s TTL
        msgs = short_ttl_inbox.pop_all("inv1")
        assert msgs == []

    def test_fresh_message_survives_alongside_stale(self, short_ttl_inbox):
        short_ttl_inbox.post(invocation_id="inv1", text="old")
        time.sleep(0.1)
        short_ttl_inbox.post(invocation_id="inv1", text="fresh")
        msgs = short_ttl_inbox.pop_all("inv1")
        assert [m.text for m in msgs] == ["fresh"]


class TestDiagnostics:

    def test_peek_count(self, inbox):
        assert inbox.peek_count("inv1") == 0
        inbox.post(invocation_id="inv1", text="m1")
        inbox.post(invocation_id="inv1", text="m2")
        assert inbox.peek_count("inv1") == 2
        inbox.pop_all("inv1")
        assert inbox.peek_count("inv1") == 0

    def test_clear(self, inbox):
        inbox.post(invocation_id="inv1", text="m1")
        inbox.post(invocation_id="inv1", text="m2")
        inbox.clear("inv1")
        assert inbox.pop_all("inv1") == []
