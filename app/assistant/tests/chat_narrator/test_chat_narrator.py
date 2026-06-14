"""Tests for ChatNarrator — manager gating, throttle/dedup, translator-agent
invocation behavior.

Translator agent itself isn't exercised here (it's an LLM agent — would
need real API calls). We mock _invoke_translator and assert ChatNarrator
calls it with the right inputs and acts on its return value.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.assistant.utils.pydantic_classes import Message


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _seed_display_names():
    """Populate the display-name registry that ChatNarrator reads from."""
    from app.assistant.chat_narrator.display_names import (
        initialize_display_name_registry,
    )
    initialize_display_name_registry({
        "web_manager": "Quimby",
        "emi_team_manager": "Em",
        "personal_admin_manager": "Phyllis",
        "devices_manager": "Watt",
    })
    yield
    initialize_display_name_registry({})


@pytest.fixture
def narrator():
    """ChatNarrator with mocked DI + mocked translator agent.

    The translator returns a deterministic short sentence by default;
    individual tests can override per test by reassigning
    ``narrator._translator_returns``.
    """
    from app.assistant.chat_narrator.chat_narrator import ChatNarrator

    with patch("app.assistant.chat_narrator.chat_narrator.DI") as mock_di:
        mock_di.event_hub.register_event = MagicMock()
        n = ChatNarrator()

        # Capture publishes.
        n._publishes = []
        def _capture(*, sender, text, reply_to=None):
            n._publishes.append((sender, text, reply_to))
        n._publish_chat = _capture

        # Mock the translator: by default returns a stub sentence.
        n._translator_returns = "Searching for the thing"
        n._invoke_translator = lambda *, display_name, manager, thinking: (
            n._translator_returns(thinking) if callable(n._translator_returns)
            else n._translator_returns
        )

        yield n


def _card(*, manager: str = "web_manager", kind: str = "planner_decision",
          what_i_am_thinking: str = "I am doing things") -> dict:
    return {
        "kind": kind,
        "manager": manager,
        "agent": f"{manager}::test",
        "headline": kind,
        "what_i_am_thinking": what_i_am_thinking,
    }


def _msg(card: dict) -> Message:
    return Message(sender="progress_curator", data=card,
                   event_topic="agent_progress_emit")


# ── Manager gating ────────────────────────────────────────────────────


class TestManagerGating:

    def test_manager_with_display_name_emits(self, narrator):
        narrator._on_card(_msg(_card(manager="web_manager",
                                      what_i_am_thinking="x")))
        assert len(narrator._publishes) == 1
        sender, text, _ = narrator._publishes[0]
        assert sender == "Quimby"
        assert "Searching for the thing" in text

    def test_manager_without_display_name_silenced(self, narrator):
        narrator._on_card(_msg(_card(manager="unconfigured_manager",
                                      what_i_am_thinking="x")))
        assert narrator._publishes == []

    def test_disabled_manager_silenced(self, narrator):
        with patch.object(
            narrator, "_narration_config_for",
            return_value={"enabled": False},
        ):
            narrator._on_card(_msg(_card(manager="web_manager",
                                          what_i_am_thinking="x")))
        assert narrator._publishes == []


# ── Card kind filtering ──────────────────────────────────────────────


class TestCardKindFiltering:

    def test_only_planner_decision_emits(self, narrator):
        for kind in ("tool_call", "tool_result", "mystery"):
            narrator._on_card(_msg(_card(kind=kind, what_i_am_thinking="x")))
        assert narrator._publishes == []

    def test_planner_decision_with_no_thinking_skipped(self, narrator):
        narrator._on_card(_msg(_card(what_i_am_thinking="")))
        assert narrator._publishes == []


# ── Translator behavior ──────────────────────────────────────────────


class TestTranslatorBehavior:

    def test_translator_empty_string_suppresses_emit(self, narrator):
        # Agent decided thinking is pure noise → empty narration → no emit.
        narrator._translator_returns = ""
        narrator._on_card(_msg(_card(what_i_am_thinking="updating my checklist")))
        assert narrator._publishes == []

    def test_translator_receives_thinking_and_display_name(self, narrator):
        captured = {}
        def capture(*, display_name, manager, thinking):
            captured.update(display_name=display_name, manager=manager, thinking=thinking)
            return "ok"
        narrator._invoke_translator = capture
        narrator._on_card(_msg(_card(
            manager="emi_team_manager",
            what_i_am_thinking="Looking up the host",
        )))
        assert captured["display_name"] == "Em"
        assert captured["manager"] == "emi_team_manager"
        assert captured["thinking"] == "Looking up the host"

    def test_runaway_long_translator_output_handled_by_caller(self, narrator):
        # ChatNarrator wraps the translator's narration in [Name] prefix —
        # if the translator is well-behaved (≤220 chars), no truncation
        # in the narrator. (Hard cap is in _invoke_translator itself,
        # mocked away here.)
        narrator._translator_returns = "x" * 200
        narrator._on_card(_msg(_card(what_i_am_thinking="...")))
        assert len(narrator._publishes) == 1
        text = narrator._publishes[0][1]
        assert text.startswith("[Quimby]")


# ── Throttle / dedup ─────────────────────────────────────────────────


class TestThrottleAndDedup:

    def test_first_card_emits(self, narrator):
        narrator._on_card(_msg(_card(what_i_am_thinking="x")))
        assert len(narrator._publishes) == 1

    def test_second_card_within_throttle_dropped(self, narrator):
        narrator._on_card(_msg(_card(what_i_am_thinking="x")))
        narrator._on_card(_msg(_card(what_i_am_thinking="y")))
        assert len(narrator._publishes) == 1

    def test_throttle_runs_BEFORE_translator_call(self, narrator):
        """Important: avoid burning LLM tokens on cards we'd suppress."""
        invocations = []
        narrator._invoke_translator = lambda *, display_name, manager, thinking: (
            invocations.append(thinking) or "ok"
        )
        narrator._on_card(_msg(_card(what_i_am_thinking="first")))
        narrator._on_card(_msg(_card(what_i_am_thinking="second-throttled")))
        # Only the first thinking made it to the translator.
        assert invocations == ["first"]

    def test_card_after_throttle_window_emits(self, narrator):
        # Distinct translations so dedup doesn't also kick in.
        outputs = iter(["first sentence", "second sentence"])
        narrator._invoke_translator = lambda *, display_name, manager, thinking: next(outputs)

        narrator._on_card(_msg(_card(what_i_am_thinking="first")))
        manager_key = "web_manager"
        old_ts, old_text = narrator._last_emit[manager_key]
        narrator._last_emit[manager_key] = (old_ts - 100.0, old_text)
        narrator._on_card(_msg(_card(what_i_am_thinking="second")))
        assert len(narrator._publishes) == 2

    def test_identical_translation_dropped_after_throttle(self, narrator):
        # Translator returns the same sentence twice; dedup catches it
        # even though the throttle window has expired.
        narrator._invoke_translator = lambda *, display_name, manager, thinking: "same line"
        narrator._on_card(_msg(_card(what_i_am_thinking="x")))
        manager_key = "web_manager"
        old_ts, old_text = narrator._last_emit[manager_key]
        narrator._last_emit[manager_key] = (old_ts - 100.0, old_text)
        narrator._on_card(_msg(_card(what_i_am_thinking="y")))
        assert len(narrator._publishes) == 1

    def test_different_managers_dont_throttle_each_other(self, narrator):
        narrator._on_card(_msg(_card(manager="web_manager",
                                      what_i_am_thinking="x")))
        narrator._on_card(_msg(_card(manager="emi_team_manager",
                                      what_i_am_thinking="y")))
        assert len(narrator._publishes) == 2
        senders = {p[0] for p in narrator._publishes}
        assert senders == {"Quimby", "Em"}


# ── Display-name lookup ─────────────────────────────────────────────


class TestInvocationStartedHandler:

    def _evt(
        self,
        manager_name: str,
        *,
        reply_to=None,
        base_display_name=None,
        display_name=None,
    ) -> Message:
        return Message(
            sender="manager_invoker",
            data={
                "manager_name": manager_name,
                "base_display_name": base_display_name,
                "display_name": display_name,
                "reply_to": reply_to,
            },
            event_topic="manager_invocation_started",
        )

    def test_named_worker_announces_delegation(self, narrator):
        narrator._on_invocation_started(self._evt("emi_team_manager"))
        assert len(narrator._publishes) == 1
        sender, text, _ = narrator._publishes[0]
        assert sender == "Em"  # seeded fixture maps emi_team_manager → Em
        assert text == "Delegating to Em."

    def test_reply_to_propagates_to_publish_call(self, narrator):
        rt = {"type": "slack", "channel_id": "C12345", "thread_ts": "1234.5"}
        narrator._on_invocation_started(
            self._evt("web_manager", reply_to=rt),
        )
        assert len(narrator._publishes) == 1
        _, _, captured_reply_to = narrator._publishes[0]
        assert captured_reply_to == rt

    def test_unnamed_manager_skipped(self, narrator):
        narrator._on_invocation_started(self._evt("dayflow_orchestrator_manager"))
        assert narrator._publishes == []

    def test_unnamed_manager_with_numbered_alias_still_skipped(self, narrator):
        # MAMInstanceManager assigns Webby_2-style suffixes for collision
        # detection even on unnamed managers (room_manager → "room_manager_2"
        # if two are running). The narrator must gate by BASE name, not
        # the suffixed instance name, so these don't leak into chat as
        # "[room_manager_2] Delegating to room_manager_2."
        narrator._on_invocation_started(self._evt(
            "room_manager",
            base_display_name="room_manager",
            display_name="room_manager_2",
        ))
        assert narrator._publishes == []

    def test_named_worker_with_suffix_uses_suffix_in_chat(self, narrator):
        # Two emi_team_managers running in the same room: second one
        # registered with display_name="Em_2" by the registry.
        narrator._on_invocation_started(self._evt(
            "emi_team_manager",
            base_display_name="Em",
            display_name="Em_2",
        ))
        assert len(narrator._publishes) == 1
        sender, text, _ = narrator._publishes[0]
        assert sender == "Em_2"
        assert text == "Delegating to Em_2."

    def test_empty_event_skipped(self, narrator):
        narrator._on_invocation_started(Message(sender="x"))
        assert narrator._publishes == []

    def test_disabled_manager_skipped(self, narrator):
        with patch.object(
            narrator, "_narration_config_for",
            return_value={"enabled": False},
        ):
            narrator._on_invocation_started(self._evt("web_manager"))
        assert narrator._publishes == []


class TestDisplayNameLookup:

    def test_known_managers_get_curated_names(self, narrator):
        assert narrator._display_name_for("web_manager") == "Quimby"
        assert narrator._display_name_for("emi_team_manager") == "Em"
        assert narrator._display_name_for("personal_admin_manager") == "Phyllis"

    def test_unknown_manager_returns_raw_name(self, narrator):
        assert narrator._display_name_for("brand_new_manager") == "brand_new_manager"

    def test_empty_manager_returns_empty(self, narrator):
        assert narrator._display_name_for("") == ""
