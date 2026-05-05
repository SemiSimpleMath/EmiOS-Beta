"""ChatNarrator — live in-chat narration of long-running sub-managers.

Subscribes to ``agent_progress_emit`` (curated cards from ProgressCurator)
and writes a short, named, chat-style update into master_room. Each
long-running sub-manager has a display name configured in its
``manager_config.yaml`` ("Quimby" for web_manager, "Em" for emi_team,
etc.) and a per-manager narration config:

  display_name: "Quimby"
  narration:
    enabled: true        # default true when display_name is set
    max_sentences: 2     # default 2 (1 = punchier, 3 = chattier)
    drop_phrases:        # added to the global noise list
      - "search_web"

Managers without a ``display_name`` are silenced entirely — explicit
opt-in keeps every new manager from leaking into chat by default.

Sentence-level filtering: each sentence in the planner's
``what_i_am_thinking`` is dropped if it contains a skip-phrase
(case-insensitive substring match). Skip-phrases include planner
meta-talk like "the critic", "action_count", "return_control" — things
that are real to the planner but noise to the user. Survivors are
joined and capped at ``max_sentences``. If everything filters out, the
emit is silently suppressed.

The pipeline upstream is unchanged:
  agents / control nodes → "agent_progress_fact"
  ProgressCurator       → "agent_progress_emit" (curated cards)
  ChatNarrator (here)   → master_room chat (UserMessage via socket_emit)
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.chat_narrator.display_names import display_name_for
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    UserMessage,
    UserMessageData,
)

logger = get_logger(__name__)


# Per-manager throttle: don't narrate more than one card every N seconds
# from the same manager. Long planner loops fire many cards; chat would
# be unreadable without this.
_THROTTLE_SECONDS = 8.0

# Default narration verbosity (sentences). Per-manager
# ``narration.max_sentences`` overrides this.
_DEFAULT_MAX_SENTENCES = 2

# Sentences containing any of these substrings (case-insensitive) get
# dropped before narration. The planner uses these terms internally to
# refer to its own scaffolding — useful for the planner, noise to the
# user. Per-manager ``narration.drop_phrases`` extends this list.
_DEFAULT_DROP_PHRASES: tuple[str, ...] = (
    "the critic",
    "per the critic",
    "critic correctly",
    "critic pointed",
    "action_count",
    "action count",
    "return_control",
    "exit_node",
    "manager_exit",
    "checklist-wise",
    "wrap up",
    "wrap-up",
    "i can end",
)


class ChatNarrator:
    """Subscribes to agent_progress_emit and posts brief, named chat updates."""

    EMIT_TOPIC = "agent_progress_emit"
    CHAT_ROOM_ID = "master_room"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # manager_name → (last_emit_ts, last_sentence)
        self._last_emit: Dict[str, tuple[float, str]] = {}

        DI.event_hub.register_event(self.EMIT_TOPIC, self._on_card)
        logger.info(
            "✅ ChatNarrator initialized (subscribed to %s, target room=%s).",
            self.EMIT_TOPIC, self.CHAT_ROOM_ID,
        )

    def _on_card(self, message: Message) -> None:
        """Translate one curated card into a chat-style narration message."""
        try:
            card = message.data if isinstance(getattr(message, "data", None), dict) else {}
            if not card:
                return

            manager = str(card.get("manager") or "").strip()
            display_name = self._display_name_for(manager)
            # No display_name in the manager's config → silent. Explicit
            # opt-in keeps every new manager from leaking into chat.
            if not display_name or display_name == manager:
                return

            cfg = self._narration_config_for(manager)
            if not cfg["enabled"]:
                return

            sentence = self._render_sentence(card, display_name, cfg)
            if not sentence:
                return

            now = time.monotonic()
            throttle_key = manager or display_name or "anon"
            with self._lock:
                last = self._last_emit.get(throttle_key)
                if last is not None:
                    last_ts, last_sentence = last
                    if (now - last_ts) < _THROTTLE_SECONDS:
                        return
                    if sentence == last_sentence:
                        # Avoid duplicate verbatim narrations even after throttle.
                        return
                self._last_emit[throttle_key] = (now, sentence)

            self._publish_chat(sender=display_name, text=sentence)
        except Exception:
            logger.debug("ChatNarrator failed to process card", exc_info=True)

    def _narration_config_for(self, manager_name: str) -> Dict[str, Any]:
        """Resolve the manager's narration config from its manager_config.yaml.

        Per-manager fields override defaults; ``drop_phrases`` extends the
        global default list rather than replacing it.
        """
        try:
            registry = getattr(DI, "manager_registry", None)
            mgr_cfg = registry.get(manager_name) if registry is not None else None
        except Exception:
            mgr_cfg = None
        narration = {}
        if isinstance(mgr_cfg, dict):
            n = mgr_cfg.get("narration")
            if isinstance(n, dict):
                narration = n
        max_sentences = narration.get("max_sentences", _DEFAULT_MAX_SENTENCES)
        try:
            max_sentences = max(1, int(max_sentences))
        except (TypeError, ValueError):
            max_sentences = _DEFAULT_MAX_SENTENCES
        extra_drops = narration.get("drop_phrases") or []
        drop_phrases = list(_DEFAULT_DROP_PHRASES) + [
            str(p).lower() for p in extra_drops if isinstance(p, str) and p.strip()
        ]
        return {
            "enabled": bool(narration.get("enabled", True)),
            "max_sentences": max_sentences,
            "drop_phrases": drop_phrases,
        }

    def _display_name_for(self, manager_name: str) -> str:
        return display_name_for(manager_name)

    def _render_sentence(
        self,
        card: Dict[str, Any],
        display_name: str,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Compose a brief narration sentence from a curated card.

        Strategy: narrate ONLY ``planner_decision`` cards, using the planner's
        own ``what_i_am_thinking`` line, filtered to drop noise sentences and
        capped at ``cfg.max_sentences``.

        ``tool_call`` and ``tool_result`` cards are intentionally skipped —
        the planner_decision that PRECEDED the tool already told the user
        what was about to happen, and the next planner_decision will tell
        them what was learned.
        """
        kind = str(card.get("kind") or "").strip()
        if kind != "planner_decision":
            return ""

        if cfg is None:
            cfg = {
                "max_sentences": _DEFAULT_MAX_SENTENCES,
                "drop_phrases": list(_DEFAULT_DROP_PHRASES),
            }

        thinking = str(card.get("what_i_am_thinking") or "").strip()
        if thinking:
            narration = self._first_sentences(
                thinking,
                max_chars=200,
                max_sentences=cfg["max_sentences"],
                drop_phrases=cfg["drop_phrases"],
            )
            if not narration:
                # Everything was filtered out as noise — silently suppress
                # rather than emit a low-signal fallback.
                return ""
            return f"[{display_name}] {narration}"

        # Fallback when the planner didn't fill what_i_am_thinking — keep
        # the user informed something is happening, but lower-signal.
        goal = str(card.get("goal") or "").strip()
        next_block = card.get("next") if isinstance(card.get("next"), dict) else {}
        next_action = str(next_block.get("action") or "").strip() if next_block else ""
        goal_short = goal if len(goal) <= 80 else goal[:77] + "..."
        if next_action and goal_short:
            return f"[{display_name}] working on: {goal_short} — next: {next_action}"
        if next_action:
            return f"[{display_name}] next: {next_action}"
        if goal_short:
            return f"[{display_name}] working on: {goal_short}"
        return ""

    @staticmethod
    def _first_sentences(
        text: str,
        *,
        max_chars: int = 200,
        max_sentences: int = 2,
        drop_phrases: Optional[list[str]] = None,
    ) -> str:
        """Extract leading sentences for chat narration.

        - Normalizes whitespace.
        - Splits on ``.``, ``!``, or ``?`` followed by whitespace.
        - Drops any sentence containing a substring from ``drop_phrases``
          (case-insensitive) — used to filter planner meta-talk that's
          internally meaningful but useless to the user.
        - Concatenates surviving sentences in order, stopping when adding
          the next would exceed ``max_chars`` OR when ``max_sentences``
          have been included.
        - If the first surviving sentence alone exceeds ``max_chars``,
          hard-truncates with ``...``.

        Returns ``""`` if every sentence was filtered out.
        """
        text = (text or "").strip()
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        parts = re.split(r"(?<=[.!?])\s+", text)
        if not parts:
            return text

        drops_lower = [p.lower() for p in (drop_phrases or []) if p]

        out = ""
        kept = 0
        for sentence in parts:
            sentence = sentence.strip()
            if not sentence:
                continue
            if drops_lower and any(p in sentence.lower() for p in drops_lower):
                continue  # noise — skip this sentence
            candidate = f"{out} {sentence}".strip() if out else sentence
            if len(candidate) > max_chars:
                if not out:
                    return sentence[: max_chars - 3].rstrip() + "..."
                break
            out = candidate
            kept += 1
            if kept >= max_sentences:
                break
        return out

    def _publish_chat(self, *, sender: str, text: str) -> None:
        """Build a UserMessage and publish via socket_emit to master_room."""
        try:
            user_msg = UserMessage(
                data_type="user_msg",
                sub_data_type=["chat_narration"],
                sender=sender,
                receiver=None,
                role="assistant",
                content=text,
                timestamp=datetime.now(timezone.utc),
                event_topic="socket_emit",
                metadata={
                    "reply_to": {"type": "socketio", "room_id": self.CHAT_ROOM_ID},
                },
                user_message_data=UserMessageData(
                    chat=text,
                    importance=1,
                    generic_type="chat_narration",
                ),
            )
            DI.event_hub.publish(user_msg)
            logger.info(
                "[chat_narrator] emitted narration sender=%r len=%d",
                sender, len(text),
            )
        except Exception:
            logger.warning("ChatNarrator publish_chat failed", exc_info=True)
