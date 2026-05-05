from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message as PersistMessage

logger = get_logger(__name__)


class ChatTaskRouterNode(ControlNode):
    """
    Deterministic handoff between room chat gate and switchboard path.

    Flow:
    - Direct chat: chat_response → final_answer_node (emits to user)
    - Handoff: chat_response ack sent here → switchboard → tool → formatter → final_answer_node (emits tool result)
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        source_agent = self._required_str(cfg, "source_agent")
        switchboard_agent = self._required_str(cfg, "switchboard_agent")
        final_node = self._required_str(cfg, "final_node")
        handoff_flag_key = self._optional_str(cfg, "exit_flag_key", "handoff_tf")
        no_op_flag_key = self._optional_str(cfg, "no_op_flag_key", "no_op_tf")
        chat_response_key = self._optional_str(cfg, "chat_response_key", "chat_response")
        switchboard_task_key = self._optional_str(cfg, "switchboard_task_key", "switchboard_task")
        switchboard_information_key = self._optional_str(cfg, "switchboard_information_key", "switchboard_information")
        no_chat_path = bool(cfg.get("no_chat_path", False))

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != source_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={source_agent} before chat-task route, got: {last_agent!r}"
            )

        no_op_tf = bool(self.blackboard.get_state_value(no_op_flag_key, False))
        should_handoff = bool(self.blackboard.get_state_value(handoff_flag_key, False))

        true_count = sum([no_op_tf, should_handoff])
        if true_count > 1:
            raise ValueError(
                f"[{self.name}] at most one of {no_op_flag_key}, {handoff_flag_key} may be true (got {true_count})."
            )

        # ── No-op ──────────────────────────────────────────────────
        if no_op_tf:
            self.blackboard.update_state_value(
                "result",
                {
                    "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                    "final_answer_answer": "",
                    "final_answer_no_op": True,
                    "final_answer_what_was_done": "No-op at chat gate.",
                    "final_answer_interesting_info": "",
                    "final_answer_sources": [],
                },
            )
            self.blackboard.update_state_value("next_agent", final_node)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # ── Direct chat reply ──────────────────────────────────────
        if not should_handoff:
            if no_chat_path:
                raise ValueError(
                    f"[{self.name}] no_chat_path=true requires explicit no_op_tf=true or {handoff_flag_key}=true."
                )

            chat_response = self.blackboard.get_state_value(chat_response_key, "")
            if not isinstance(chat_response, str) or not chat_response.strip():
                raise ValueError(
                    f"[{self.name}] {chat_response_key!r} must be a non-empty string when {handoff_flag_key}=false."
                )
            self.blackboard.update_state_value(
                "result",
                {
                    "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                    "final_answer_answer": chat_response.strip(),
                    "final_answer_what_was_done": "Answered directly in chat gate.",
                    "final_answer_interesting_info": "",
                    "final_answer_sources": [],
                },
            )
            self.blackboard.update_state_value("next_agent", final_node)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # ── Handoff to switchboard ─────────────────────────────────
        switchboard_task = self.blackboard.get_state_value(switchboard_task_key, "")
        if not isinstance(switchboard_task, str) or not switchboard_task.strip():
            raise ValueError(
                f"[{self.name}] {switchboard_task_key!r} must be a non-empty string when {handoff_flag_key}=true."
            )
        switchboard_information = self.blackboard.get_state_value(switchboard_information_key, "")
        if switchboard_information is None:
            switchboard_information = ""
        if not isinstance(switchboard_information, str):
            raise ValueError(f"[{self.name}] {switchboard_information_key!r} must be a string when provided.")

        self.blackboard.update_state_value("task", switchboard_task.strip())
        self.blackboard.update_state_value("information", switchboard_information.strip())
        self.blackboard.update_state_value("result", None)

        # Send ack to user before tool work starts.
        chat_response = str(self.blackboard.get_state_value(chat_response_key, "") or "").strip() or "On it."
        self._send_ack_to_user(chat_response)

        self.blackboard.update_state_value("next_agent", switchboard_agent)
        self.blackboard.update_state_value("last_agent", self.name)

    # ------------------------------------------------------------------
    # Shared helpers (inherited by MasterRoomChatTaskRouterNode)
    # ------------------------------------------------------------------

    def _send_ack_to_user(self, text: str) -> None:
        """Send an acknowledgment to the user through the normal transport layer.

        Persists the message to blackboard for chat history, then dispatches
        via ``DI.outbound_chat_publisher`` which routes to the right surface
        based on ``reply_to.type``. ``reply_to`` itself is read from the
        canonical home — ``scope_context.reply_to`` (set at ingress and
        propagated through chained sub-managers).
        """
        if not text.strip():
            return

        assistant_name = get_required_assistant_name()
        now_utc = datetime.now(timezone.utc)
        request_id = self.blackboard.get_request_id() or ""

        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        room_surface = str(self.blackboard.get_state_value("room_surface", "") or "").strip().lower()
        room_context_id = str(self.blackboard.get_state_value("room_context_id", "") or "main").strip() or "main"
        room_contact_name = str(self.blackboard.get_state_value("room_contact_name", "") or "").strip()
        room_policy_id = str(
            self.blackboard.get_state_value("room_policy_id", "") or f"room_policy::{room_id}::v1"
        ).strip()
        visibility = "owner_only" if room_surface == "ui" else "room_shared"

        # 1. Persist to blackboard (chat history).
        outbound_msg = PersistMessage(
            data_type="user_msg",
            sender=assistant_name,
            role="assistant",
            content=text,
            is_chat=True,
            request_id=request_id.strip() or None,
            timestamp=now_utc,
            room_id=room_id or None,
            room_surface=room_surface or None,
            room_context_id=room_context_id,
            room_visibility=visibility,
            room_policy_id=room_policy_id or None,
            room_message_direction="outbound",
            room_initiated_by="agent",
            room_delivery_mode="auto_send",
            room_speaker_id=f"assistant:{assistant_name.lower()}",
            room_speaker_name=assistant_name,
            room_speaker_role="assistant",
            room_actor_id=f"assistant:{assistant_name.lower()}",
            data={
                "room_id": room_id,
                "room_contact_name": room_contact_name,
                "room_surface": room_surface,
            },
        )
        DI.global_blackboard.add_msg(outbound_msg)

        # 2. Send to user via the outbound publisher (surface-aware).
        publisher = getattr(DI, "outbound_chat_publisher", None)
        if publisher is None:
            logger.warning(
                "[%s] outbound_chat_publisher unavailable; ack not delivered",
                self.name,
            )
            return
        publisher.publish(
            sender=assistant_name,
            text=text,
            reply_to=self._resolve_reply_to(),
            request_id=request_id.strip() or None,
        )

    def _resolve_reply_to(self) -> dict | None:
        """Pull reply_to from the canonical home — scope_context.reply_to.

        Falls back to deriving from ``room_surface`` + ``room_id``
        blackboard keys for back-compat with paths that haven't fully
        migrated to scope-based reply_to yet.
        """
        scope = self.blackboard.get_state_value("scope_context")
        if isinstance(scope, dict):
            rt = scope.get("reply_to")
            if isinstance(rt, dict) and rt:
                return dict(rt)

        # Fallback: derive a minimal reply_to from blackboard surface info.
        # Only covers the UI socketio case cleanly; slack/telegram/sms with
        # missing scope.reply_to will hit this path with insufficient
        # transport coordinates and fail loudly at the publisher.
        room_surface = str(self.blackboard.get_state_value("room_surface", "") or "").strip().lower()
        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        if room_surface == "ui" or not room_surface:
            return {"type": "socketio", "room_id": room_id} if room_id else None
        # Surface known but no scope.reply_to — log and surface None so
        # publisher drops with a clear warning.
        logger.warning(
            "[%s] no scope_context.reply_to for surface=%s; ack will be dropped",
            self.name, room_surface,
        )
        return None

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        chat_gate = flow_cfg.get("chat_gate")
        if not isinstance(chat_gate, dict):
            raise ValueError("manager_flow_config.chat_gate must be a dict.")
        return chat_gate

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"chat task router requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"chat task router key '{key}' must be non-empty when provided.")
        return raw.strip()
