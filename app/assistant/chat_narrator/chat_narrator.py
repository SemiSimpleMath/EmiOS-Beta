"""ChatNarrator — proof of concept for live in-chat narration of long tasks.

Subscribes to ``agent_progress_emit`` (already-curated cards from
ProgressCurator) and writes a short, named, chat-style update into
master_room as a user-facing message. Each long-running sub-manager (web,
emi_team, etc.) gets a display name like a member of Emi's team — "Quimby
is on it" rather than opaque "Emi is thinking".

This is the first leg of the team-naming proof of concept:
- One-way (narrator → chat). No user steering yet.
- Cheap throttle to keep chat readable (one narration per manager per N seconds,
  drop verbatim repeats).
- Hardcoded display name map for now — can move to per-manager config later.

The pipeline below is already in place and unchanged by this module:
  agents/control nodes → "agent_progress_fact"
  ProgressCurator      → "agent_progress_emit" (curated cards)
  EmiEventRelay        → UI room "progress" tab (existing side panel)

ChatNarrator adds a new subscriber to ``agent_progress_emit`` that ALSO
publishes to master_room chat as a regular Emi-style message. The
ProgressCurator's existing card output is the source of truth — we don't
re-curate, we just translate the headline into a chat sentence.
"""
from __future__ import annotations

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
            sentence = self._render_sentence(card, display_name)
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

    def _display_name_for(self, manager_name: str) -> str:
        return display_name_for(manager_name)

    def _render_sentence(self, card: Dict[str, Any], display_name: str) -> str:
        """Compose a brief narration sentence from a curated card.

        Templated for the PoC — keep it simple. A future LLM-driven version
        could synthesize more natural narration from the card content + the
        worker's voice profile.
        """
        kind = str(card.get("kind") or "").strip()
        goal = str(card.get("goal") or "").strip()
        next_block = card.get("next") if isinstance(card.get("next"), dict) else {}
        next_action = str(next_block.get("action") or "").strip() if next_block else ""
        learned = card.get("learned")
        learned_first = ""
        if isinstance(learned, list) and learned:
            learned_first = str(learned[0]).strip()

        # Goal trimming: short for chat.
        goal_short = goal if len(goal) <= 80 else goal[:77] + "..."

        if kind == "planner_decision":
            if next_action and goal_short:
                return f"[{display_name}] working on: {goal_short} — next: {next_action}"
            if next_action:
                return f"[{display_name}] next: {next_action}"
            if goal_short:
                return f"[{display_name}] working on: {goal_short}"
            return ""
        if kind == "tool_call":
            tool = str(card.get("meta", {}).get("tool") or next_action or "").strip()
            if tool and goal_short:
                return f"[{display_name}] running {tool} for: {goal_short}"
            if tool:
                return f"[{display_name}] running {tool}"
            return ""
        if kind == "tool_result":
            if learned_first:
                snippet = learned_first if len(learned_first) <= 120 else learned_first[:117] + "..."
                return f"[{display_name}] got: {snippet}"
            return ""
        # Other kinds: skip until we know what they look like.
        return ""

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
