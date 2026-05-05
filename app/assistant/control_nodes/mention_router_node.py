"""MentionRouterNode — front-of-pipeline ``@name`` parser for master_room.

Runs before chat_gate. Inspects the inbound user message; if it starts with
``@<name>`` (case-insensitive), tries to resolve the name to an active
sub-manager invocation:

  - Active worker found → posts the message body (everything after the
    ``@name``) into that invocation's steering inbox, emits a brief ack
    chat message ("→ Quimby noted"), and short-circuits to final_answer
    (skip chat_gate / switchboard / tool_caller for this exchange).

  - No active worker by that name → emits "no active <name> right now"
    chat message and short-circuits. Per design (single-chain, no auto-
    spawn): if Quimby isn't already working, ``@quimby`` does nothing.

  - No ``@`` prefix → falls through to chat_gate via the default state_map
    edge. Normal flow.

This is the "Slice 2" wedge of the team-naming architecture. Slice 3
(planner-side steering pickup) consumes the inbox.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Tuple

from app.assistant.chat_narrator.display_names import (
    display_name_for,
    manager_for_display_name,
)
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.manager_runtime.active_workers import (
    find_active_invocation_by_display_name,
    list_active_display_names,
)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    UserMessage,
    UserMessageData,
)

logger = get_logger(__name__)


# Match ``@name`` at the very start of the message. Name is alphanumeric +
# underscore (matches our display-name registry conventions). Capture the
# name and the rest as separate groups.
_MENTION_RE = re.compile(r"^\s*@([A-Za-z0-9_]+)\b\s*(.*)$", re.DOTALL)


class MentionRouterNode(ControlNode):
    """Recognize ``@name <text>`` at the head of user messages and route
    accordingly."""

    CHAT_ROOM_ID = "master_room"

    def action_handler(self, message: Message):
        self.blackboard.update_state_value("next_agent", None)

        user_text = self._extract_user_text(message)
        if not user_text:
            self.blackboard.update_state_value("last_agent", self.name)
            return  # fall through to default state_map edge → chat_gate

        match = _MENTION_RE.match(user_text)
        if not match:
            self.blackboard.update_state_value("last_agent", self.name)
            return  # not a mention; fall through

        name = match.group(1)
        body = (match.group(2) or "").strip()

        manager_name = manager_for_display_name(name)
        if manager_name is None:
            # Name isn't even in the registry. Could be @anyone — let chat_gate
            # see the original message intact; don't shortcut.
            self.blackboard.update_state_value("last_agent", self.name)
            return

        worker = find_active_invocation_by_display_name(name)
        if worker is None:
            display = display_name_for(manager_name)
            active = list_active_display_names()
            if active:
                others = ", ".join(n for n in active if n.lower() != display.lower())
                hint = f" (active right now: {others})" if others else ""
            else:
                hint = ""
            ack_text = (
                f"No active {display} right now — nothing to steer.{hint}"
            )
            self._publish_ack(sender="Em", text=ack_text)
            logger.info(
                "[%s] @%s — no active worker; sent ack.", self.name, name,
            )
            self._short_circuit_to_final_answer()
            return

        # Found an active worker. Post body to its inbox.
        if not body:
            ack_text = (
                f"You addressed {worker['display_name']} but didn't say anything "
                f"after the @ — nothing to forward."
            )
            self._publish_ack(sender="Em", text=ack_text)
            logger.info(
                "[%s] @%s with empty body; sent ack.", self.name, name,
            )
            self._short_circuit_to_final_answer()
            return

        inbox = getattr(DI, "steering_inbox", None)
        if inbox is None:
            logger.error(
                "[%s] DI.steering_inbox not registered; cannot deliver @mention.",
                self.name,
            )
            self._publish_ack(
                sender="Em",
                text="Steering inbox isn't available — message not delivered.",
            )
            self._short_circuit_to_final_answer()
            return

        posted = inbox.post(invocation_id=worker["invocation_id"], text=body)
        if posted:
            ack_text = f"→ {worker['display_name']} noted."
            logger.info(
                "[%s] forwarded @%s steering to invocation=%s len=%d",
                self.name, name, worker["invocation_id"], len(body),
            )
        else:
            ack_text = f"Could not deliver to {worker['display_name']}."
        self._publish_ack(sender=worker["display_name"], text=ack_text)
        self._short_circuit_to_final_answer()

    def _extract_user_text(self, message: Message) -> str:
        """Pull the inbound user text. Mirrors context_injector's resolution
        chain so the same input the chat_gate would see."""
        if message is None:
            return ""
        for attr in ("agent_input", "content", "task"):
            val = getattr(message, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # Fallback: the blackboard's task key (set by inbound preprocessor).
        bb_task = self.blackboard.get_state_value("task", "")
        return str(bb_task or "").strip()

    def _short_circuit_to_final_answer(self) -> None:
        """Skip the rest of the master_room pipeline for this exchange."""
        self.blackboard.update_state_value("next_agent", "final_answer_node")
        self.blackboard.update_state_value("last_agent", self.name)

    def _publish_ack(self, *, sender: str, text: str) -> None:
        """Emit a single brief chat message acknowledging the mention.

        Uses the same socket_emit path as ChatNarrator — small, named
        message into master_room scope.
        """
        try:
            user_msg = UserMessage(
                data_type="user_msg",
                sub_data_type=["mention_ack"],
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
                    generic_type="mention_ack",
                ),
            )
            DI.event_hub.publish(user_msg)
        except Exception:
            logger.warning(
                "[%s] failed to publish mention ack", self.name, exc_info=True,
            )
