"""ChatNarrator — live in-chat narration of long-running sub-managers.

Subscribes to ``agent_progress_emit`` (curated cards from ProgressCurator)
and writes a short, named, chat-style update into master_room. Each
long-running sub-manager has a display name configured in its
``manager_config.yaml`` ("Quimby" for web_manager, "Em" for emi_team,
etc.). Managers without a ``display_name`` are silenced entirely —
explicit opt-in keeps every new manager from leaking into chat.

The actual translation from planner-internal ``what_i_am_thinking`` into
chat-readable narration is done by a tiny LLM agent
(``chat_narrator::translator``, on gpt-5.4-nano). The agent's system
prompt handles voice, brevity, and noise filtering — when the input is
pure scaffolding ("updating my checklist", "per the critic, ...") it
returns an empty string and we suppress the emit.

The throttle (one emit per manager per ~8s) is applied BEFORE the LLM
call so we don't pay for tokens we'd discard. Dedup against the last
sentence runs after the LLM call.

Per-manager config in ``manager_config.yaml``:

  display_name: "Quimby"
  narration:
    enabled: true     # default true when display_name is set; false silences

The pipeline upstream is unchanged:
  agents / control nodes → "agent_progress_fact"
  ProgressCurator       → "agent_progress_emit" (curated cards)
  ChatNarrator (here)   → master_room chat (UserMessage via socket_emit)
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
# be unreadable without this. Applied BEFORE the LLM translator call so
# we don't pay for tokens we'd discard.
_THROTTLE_SECONDS = 8.0

# The translator agent that turns planner-internal thinking into chat text.
_TRANSLATOR_AGENT = "chat_narrator::translator"


class ChatNarrator:
    """Subscribes to event_hub topics and posts brief, named chat updates.

    Two subscriptions:
      ``agent_progress_emit``       — curated cards from ProgressCurator;
                                       translated to chat narration.
      ``manager_invocation_started`` — fired by ManagerInvoker each time
                                       a manager starts; emits the
                                       ``Delegating to <Name>.`` line.

    Both paths share the same display-name registry, narration-config
    lookup, and ``_publish_chat`` socket-emit helper. The narrator does
    not import any manager / invoker code — purely a listener.
    """

    EMIT_TOPIC = "agent_progress_emit"
    INVOCATION_TOPIC = "manager_invocation_started"
    CHAT_ROOM_ID = "master_room"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # manager_name → (last_emit_ts, last_sentence)
        self._last_emit: Dict[str, tuple[float, str]] = {}

        DI.event_hub.register_event(self.EMIT_TOPIC, self._on_card)
        DI.event_hub.register_event(
            self.INVOCATION_TOPIC, self._on_invocation_started,
        )
        logger.info(
            "✅ ChatNarrator initialized (topics=%s, target room=%s, translator=%s).",
            [self.EMIT_TOPIC, self.INVOCATION_TOPIC],
            self.CHAT_ROOM_ID, _TRANSLATOR_AGENT,
        )

    def _on_card(self, message: Message) -> None:
        """Translate one curated card into a chat-style narration message."""
        try:
            card = message.data if isinstance(getattr(message, "data", None), dict) else {}
            if not card:
                return

            # We narrate ONLY planner_decision cards. Tool_call / tool_result
            # don't carry the planner's voiced thought; the planner_decision
            # before/after them already covers what the user needs to know.
            if str(card.get("kind") or "").strip() != "planner_decision":
                return

            manager = str(card.get("manager") or "").strip()
            display_name = self._display_name_for(manager)
            # No display_name in the manager's config → silent.
            if not display_name or display_name == manager:
                return

            cfg = self._narration_config_for(manager)
            if not cfg["enabled"]:
                return

            thinking = str(card.get("what_i_am_thinking") or "").strip()
            if not thinking:
                return

            # Throttle gate BEFORE the LLM call — don't burn tokens on
            # cards we'd suppress anyway.
            now = time.monotonic()
            throttle_key = manager or display_name or "anon"
            with self._lock:
                last = self._last_emit.get(throttle_key)
                if last is not None and (now - last[0]) < _THROTTLE_SECONDS:
                    return

            # Ask the translator agent for a chat-friendly sentence.
            narration = self._invoke_translator(
                display_name=display_name,
                manager=manager,
                thinking=thinking,
            )
            if not narration:
                # Agent decided this thinking is pure scaffolding/noise.
                return

            sentence = f"[{display_name}] {narration}"

            # Dedup against last verbatim emit + record throttle slot.
            with self._lock:
                last = self._last_emit.get(throttle_key)
                if last is not None and sentence == last[1]:
                    return
                self._last_emit[throttle_key] = (now, sentence)

            reply_to = card.get("reply_to") if isinstance(card.get("reply_to"), dict) else None
            self._publish_chat(sender=display_name, text=sentence, reply_to=reply_to)
        except Exception:
            logger.debug("ChatNarrator failed to process card", exc_info=True)

    def _on_invocation_started(self, message: Message) -> None:
        """Handle a ``manager_invocation_started`` event.

        Publishes ``Delegating to <Name>.`` in chat for any named worker.
        Silent for unnamed workers, disabled narration, or any failure
        in the chain — never breaks the firing manager.

        Uses the per-instance ``display_name`` from the event payload
        (e.g. ``Webby_2``) rather than looking up the base name from
        config — that's how the @mention router will address it later.
        """
        try:
            data = message.data if isinstance(getattr(message, "data", None), dict) else {}
            manager_name = str(data.get("manager_name") or "").strip()
            if not manager_name:
                return
            # Per-instance display_name comes from MAMInstanceManager via
            # the event. Fall back to the base name from config only if
            # the event lacks it (older publishers / tests).
            display_name = str(data.get("display_name") or "").strip()
            if not display_name:
                display_name = display_name_for(manager_name)
            if not display_name or display_name == manager_name:
                return
            cfg = self._narration_config_for(manager_name)
            if not cfg["enabled"]:
                return
            reply_to = data.get("reply_to") if isinstance(data.get("reply_to"), dict) else None
            self._publish_chat(
                sender=display_name,
                text=f"Delegating to {display_name}.",
                reply_to=reply_to,
            )
        except Exception:
            logger.debug(
                "ChatNarrator._on_invocation_started failed", exc_info=True,
            )

    def _narration_config_for(self, manager_name: str) -> Dict[str, Any]:
        """Resolve the manager's narration config from its manager_config.yaml.

        Today only ``enabled`` is honored. Future fields (voice profile,
        per-manager prompt override, etc.) can be added here without
        touching call sites.
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
        return {
            "enabled": bool(narration.get("enabled", True)),
        }

    def _display_name_for(self, manager_name: str) -> str:
        return display_name_for(manager_name)

    def _invoke_translator(
        self, *, display_name: str, manager: str, thinking: str,
    ) -> str:
        """Run the translator agent on one ``what_i_am_thinking`` blob.

        Returns the agent's ``narration`` field (possibly empty), or empty
        on any failure — narration must never break the host process.
        """
        try:
            agent = DI.agent_factory.create_agent(_TRANSLATOR_AGENT)
            if agent is None:
                logger.debug("Translator agent %s not available", _TRANSLATOR_AGENT)
                return ""
            information = (
                f"Worker display name: {display_name}\n"
                f"Manager (internal): {manager}"
            )
            # Direct invocation (no manager request_handler) — pass via
            # ``agent_input`` so AgentInputApplier writes ``task`` and
            # ``information`` onto the agent's blackboard. Setting
            # ``message.task`` / ``message.information`` directly would NOT
            # work — those fields are only consumed by MultiAgentManager,
            # not by the agent activation path.
            result = agent.action_handler(
                Message(agent_input={
                    "task": thinking,
                    "information": information,
                })
            )
            data = getattr(result, "data", None)
            if not isinstance(data, dict):
                return ""
            narration = str(data.get("narration") or "").strip()
            # Hard guard against runaway long output even if the LLM
            # ignored the contract.
            if len(narration) > 220:
                narration = narration[:217].rstrip() + "..."
            return narration
        except Exception:
            logger.debug("translator agent invocation failed", exc_info=True)
            return ""

    def _publish_chat(
        self, *, sender: str, text: str, reply_to: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Route the narration to the originating surface.

        ``reply_to`` carries the surface's transport coordinates (set at
        ingress, propagated through ScopeContext). When None, falls back
        to the default UI socket. Surface-specific dispatch lives in
        small handler methods so adding a transport is one new branch.
        """
        try:
            effective = reply_to or {"type": "socketio", "room_id": self.CHAT_ROOM_ID}
            surface_type = str(effective.get("type") or "").strip().lower()
            if surface_type == "socketio":
                self._publish_chat_socketio(sender=sender, text=text, reply_to=effective)
            elif surface_type == "slack":
                self._publish_chat_slack(sender=sender, text=text, reply_to=effective)
            else:
                logger.warning(
                    "[chat_narrator] no outbound handler for surface=%r — dropping narration",
                    surface_type,
                )
                return
            logger.info(
                "[chat_narrator] emitted narration sender=%r surface=%s len=%d",
                sender, surface_type, len(text),
            )
        except Exception:
            logger.warning("ChatNarrator publish_chat failed", exc_info=True)

    def _publish_chat_socketio(
        self, *, sender: str, text: str, reply_to: Dict[str, Any],
    ) -> None:
        """UI socketio path — publish a UserMessage to the socket_emit
        relay (existing chain). EmiEventRelay routes to the right room.
        """
        user_msg = UserMessage(
            data_type="user_msg",
            sub_data_type=["chat_narration"],
            sender=sender,
            receiver=None,
            role="assistant",
            content=text,
            timestamp=datetime.now(timezone.utc),
            event_topic="socket_emit",
            metadata={"reply_to": reply_to},
            user_message_data=UserMessageData(
                chat=text,
                importance=1,
                generic_type="chat_narration",
            ),
        )
        DI.event_hub.publish(user_msg)

    def _publish_chat_slack(
        self, *, sender: str, text: str, reply_to: Dict[str, Any],
    ) -> None:
        """Slack path — call slack_transport's send_reply directly.

        Slack outbound has no event_hub topic today. Worker name is
        embedded in the body since slack doesn't render arbitrary
        sender names per-message in a thread.
        """
        channel_id = str(reply_to.get("channel_id") or "").strip()
        thread_ts = str(reply_to.get("thread_ts") or "").strip()
        if not channel_id:
            logger.warning("[chat_narrator] slack reply_to missing channel_id; dropping")
            return
        slack_transport = getattr(DI, "slack_transport", None)
        if slack_transport is None:
            logger.warning("[chat_narrator] slack_transport not available in DI; dropping")
            return
        body = f"[{sender}] {text}" if not text.startswith(f"[{sender}]") else text
        try:
            slack_transport.send_reply(
                channel_id=channel_id, body=body, thread_ts=thread_ts,
            )
        except Exception:
            logger.warning(
                "[chat_narrator] slack send_reply failed (channel=%s)", channel_id,
                exc_info=True,
            )
