"""Tests for ChatNarrator — sentence translation + throttle + dedup.

ChatNarrator subscribes to agent_progress_emit and writes brief named
narrations to master_room chat. These tests exercise the translation
function directly + the throttle/dedup behavior of _on_card without
needing a live event_hub.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from app.assistant.utils.pydantic_classes import Message


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _seed_display_names():
    """Populate the display-name registry that ChatNarrator reads from.

    Production fills this at boot from manager configs; tests need it set
    explicitly because we don't bootstrap the full system here.
    """
    from app.assistant.chat_narrator.display_names import (
        initialize_display_name_registry,
    )
    initialize_display_name_registry({
        "web_manager": "Quimby",
        "emi_team_manager": "Em",
        "personal_admin_manager": "Phyllis",
        "devices_manager": "Watt",
        "kg_team_manager": "Mnemo",
    })
    yield
    initialize_display_name_registry({})


@pytest.fixture
def narrator():
    """ChatNarrator with mocked DI (event_hub registration is no-op'd
    so the constructor doesn't try to wire into real services)."""
    from app.assistant.chat_narrator.chat_narrator import ChatNarrator

    with patch("app.assistant.chat_narrator.chat_narrator.DI") as mock_di:
        mock_di.event_hub.register_event = MagicMock()
        n = ChatNarrator()
        # Track all publish calls for assertions.
        n._publishes = []
        n._original_publish = n._publish_chat
        n._publish_chat = lambda *, sender, text: n._publishes.append((sender, text))
        yield n


def _card(*, manager: str = "web_manager", kind: str = "tool_call",
          goal: str = "", next_action: str = "",
          tool: str = "", learned=None) -> dict:
    return {
        "kind": kind,
        "manager": manager,
        "agent": f"{manager}::test",
        "headline": kind,
        "goal": goal,
        "learned": learned or [],
        "next": {"action": next_action, "input_preview": ""},
        "meta": {"tool": tool},
    }


def _msg(card: dict) -> Message:
    return Message(sender="progress_curator", data=card, event_topic="agent_progress_emit")


# ── Sentence rendering ─────────────────────────────────────────────


class TestSentenceRendering:

    def test_planner_decision_with_goal_and_next(self, narrator):
        s = narrator._render_sentence(
            _card(kind="planner_decision", goal="Find Porto's savory items",
                  next_action="search_web"),
            "Quimby",
        )
        assert s == "[Quimby] working on: Find Porto's savory items — next: search_web"

    def test_planner_decision_with_only_next(self, narrator):
        s = narrator._render_sentence(
            _card(kind="planner_decision", next_action="search_web"),
            "Quimby",
        )
        assert s == "[Quimby] next: search_web"

    def test_tool_call_with_tool_and_goal(self, narrator):
        s = narrator._render_sentence(
            _card(kind="tool_call", tool="search_web", goal="Porto's menu"),
            "Quimby",
        )
        assert s == "[Quimby] running search_web for: Porto's menu"

    def test_tool_result_with_learned(self, narrator):
        s = narrator._render_sentence(
            _card(kind="tool_result", learned=["Found 12 savory items"]),
            "Quimby",
        )
        assert s == "[Quimby] got: Found 12 savory items"

    def test_unknown_kind_returns_empty(self, narrator):
        s = narrator._render_sentence(_card(kind="mystery_kind"), "Quimby")
        assert s == ""

    def test_long_goal_truncated(self, narrator):
        long_goal = "x" * 200
        s = narrator._render_sentence(
            _card(kind="planner_decision", goal=long_goal, next_action="continue"),
            "Quimby",
        )
        # Truncation kicks in for goals > 80 chars (yielding 77 + "...")
        assert "..." in s
        assert len(s) < 200


# ── Display name lookup ─────────────────────────────────────────────


class TestDisplayNameLookup:

    def test_known_managers_get_curated_names(self, narrator):
        assert narrator._display_name_for("web_manager") == "Quimby"
        assert narrator._display_name_for("emi_team_manager") == "Em"
        assert narrator._display_name_for("personal_admin_manager") == "Phyllis"
        assert narrator._display_name_for("devices_manager") == "Watt"
        assert narrator._display_name_for("kg_team_manager") == "Mnemo"

    def test_unknown_manager_falls_back_to_humanized_name(self, narrator):
        # "_manager" suffix stripped, underscores → spaces, title-cased.
        assert narrator._display_name_for("brand_new_manager") == "Brand New"

    def test_empty_manager_falls_back_to_em(self, narrator):
        assert narrator._display_name_for("") == "Em"


# ── Throttle + dedup ─────────────────────────────────────────────


class TestThrottleAndDedup:

    def test_first_card_emits(self, narrator):
        narrator._on_card(_msg(_card(kind="tool_call", tool="search_web", goal="Porto's")))
        assert len(narrator._publishes) == 1
        sender, text = narrator._publishes[0]
        assert sender == "Quimby"
        assert "running search_web" in text

    def test_second_card_within_throttle_dropped(self, narrator):
        narrator._on_card(_msg(_card(kind="tool_call", tool="t1", goal="g1")))
        narrator._on_card(_msg(_card(kind="tool_call", tool="t2", goal="g2")))
        assert len(narrator._publishes) == 1  # second was throttled

    def test_card_after_throttle_window_emits(self, narrator):
        narrator._on_card(_msg(_card(kind="tool_call", tool="t1", goal="g1")))
        # Manually advance the throttle bookkeeping back in time so a
        # second emit can land without sleeping.
        manager_key = "web_manager"
        old_ts, old_text = narrator._last_emit[manager_key]
        narrator._last_emit[manager_key] = (old_ts - 100.0, old_text)

        narrator._on_card(_msg(_card(kind="tool_call", tool="t2", goal="g2")))
        assert len(narrator._publishes) == 2

    def test_identical_sentence_dropped_even_after_throttle(self, narrator):
        card = _card(kind="tool_call", tool="search_web", goal="Porto's")
        narrator._on_card(_msg(card))
        # Force throttle to expire.
        manager_key = "web_manager"
        old_ts, old_text = narrator._last_emit[manager_key]
        narrator._last_emit[manager_key] = (old_ts - 100.0, old_text)

        narrator._on_card(_msg(card))
        # Still only one publish — verbatim dedup catches it.
        assert len(narrator._publishes) == 1

    def test_different_managers_dont_throttle_each_other(self, narrator):
        narrator._on_card(_msg(_card(manager="web_manager", kind="tool_call",
                                     tool="search_web", goal="Porto's")))
        narrator._on_card(_msg(_card(manager="devices_manager", kind="tool_call",
                                     tool="set_thermostat", goal="cool to 70F")))
        assert len(narrator._publishes) == 2
        senders = {p[0] for p in narrator._publishes}
        assert senders == {"Quimby", "Watt"}

    def test_empty_card_skipped(self, narrator):
        narrator._on_card(Message(sender="progress_curator", data={}))
        assert narrator._publishes == []

    def test_unrenderable_kind_skipped_silently(self, narrator):
        narrator._on_card(_msg(_card(kind="something_weird")))
        assert narrator._publishes == []
