from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.assistant.control_nodes.chat_task_router_node import ChatTaskRouterNode
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.message_manager.save_to_unified_db import save_to_unified_db
from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message as PersistMessage

logger = get_logger(__name__)


class MasterRoomChatTaskRouterNode(ChatTaskRouterNode):
    """
    Master-room specialized chat-task router.

    Adds dayflow delegation and guard persistence on top of the base
    chat/handoff pattern.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        source_agent = self._required_str(cfg, "source_agent")
        final_node = self._required_str(cfg, "final_node")
        handoff_flag_key = self._optional_str(cfg, "exit_flag_key", "handoff_tf")
        no_op_flag_key = self._optional_str(cfg, "no_op_flag_key", "no_op_tf")
        chat_response_key = self._optional_str(cfg, "chat_response_key", "chat_response")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != source_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={source_agent} before chat-task route, got: {last_agent!r}"
            )

        no_op_tf = bool(self.blackboard.get_state_value(no_op_flag_key, False))
        should_handoff = bool(self.blackboard.get_state_value(handoff_flag_key, False))
        dayflow_delegate = bool(self.blackboard.get_state_value("dayflow_delegate_tf", False))

        true_count = sum([no_op_tf, should_handoff, dayflow_delegate])
        if true_count > 1:
            raise ValueError(
                f"[{self.name}] at most one of {no_op_flag_key}, {handoff_flag_key}, "
                f"dayflow_delegate_tf may be true (got {true_count})."
            )

        # ── Dayflow delegation (master_room only) ──────────────────
        if dayflow_delegate:
            task_desc = str(
                self.blackboard.get_state_value("dayflow_task_description", "") or ""
            ).strip()
            if not task_desc:
                raise ValueError(
                    f"[{self.name}] dayflow_task_description required when dayflow_delegate_tf=true."
                )

            self._persist_dayflow_delegation(task_desc)

            chat_response = str(
                self.blackboard.get_state_value(chat_response_key, "") or ""
            ).strip() or "Got it - I'll handle that."

            self._send_ack_to_user(chat_response)
            self._persist_guard(guard_label=f"Task: {task_desc}")

            # final_answer_node exits silently — ack was already sent.
            self.blackboard.update_state_value(
                "result",
                {
                    "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                    "final_answer_answer": "",
                    "final_answer_no_op": True,
                    "final_answer_what_was_done": f"Delegated to dayflow orchestrator: {task_desc}",
                    "final_answer_interesting_info": "",
                    "final_answer_sources": [],
                },
            )
            logger.info("[%s] dayflow delegation - task: %s", self.name, task_desc)
            self.blackboard.update_state_value("next_agent", final_node)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # ── No-op, direct chat, handoff — handled by base class ────
        # Base adds guard for handoffs via _on_handoff hook.
        super().action_handler(message)

        # If base chose handoff, persist a guard message.
        if should_handoff:
            switchboard_task = str(self.blackboard.get_state_value("task", "") or "").strip()
            switchboard_information = str(self.blackboard.get_state_value("information", "") or "").strip()
            self._persist_guard(
                guard_label=f"Task: {switchboard_task}\nArgs: {switchboard_information or '(none)'}",
            )

    # ------------------------------------------------------------------
    # Master-room specific helpers
    # ------------------------------------------------------------------

    def _persist_guard(self, *, guard_label: str) -> None:
        """Persist an in-flight guard message to blackboard + unified DB."""
        assistant_name = get_required_assistant_name()
        now_utc = datetime.now(timezone.utc)
        request_id = self.blackboard.get_request_id() or ""

        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        room_surface = str(self.blackboard.get_state_value("room_surface", "") or "").strip().lower()
        room_context_id = str(self.blackboard.get_state_value("room_context_id", "") or "main").strip() or "main"
        room_policy_id = str(
            self.blackboard.get_state_value("room_policy_id", "") or f"room_policy::{room_id}::v1"
        ).strip()
        visibility = "owner_only" if room_surface == "ui" else "room_shared"

        guard_text = f"[THIS TASK IS ALREADY IN FLIGHT - DO NOT RE-ATTEMPT]\n{guard_label}"

        guard_msg = PersistMessage(
            data_type="agent_msg",
            sub_data_type=["agent_guard"],
            sender=assistant_name,
            role="assistant",
            content=guard_text,
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
            room_delivery_mode="no_send",
            room_speaker_id=f"assistant:{assistant_name.lower()}",
            room_speaker_name=assistant_name,
            room_speaker_role="assistant",
            room_actor_id=f"assistant:{assistant_name.lower()}",
            data={"room_id": room_id, "room_surface": room_surface, "agent_guard": True},
        )
        DI.global_blackboard.add_msg(guard_msg)

        if room_id:
            source = f"room_{room_surface}" if room_surface else "room_system"
            guard_payload = {
                "id": guard_msg.id,
                "timestamp": now_utc,
                "role": "assistant",
                "message": guard_text,
                "room_id": room_id,
                "room_surface": room_surface or None,
                "room_context_id": room_context_id,
                "room_message_direction": "outbound",
                "room_speaker_name": assistant_name,
                "metadata_json": {"sub_data_type": ["agent_guard"], "agent_guard": True},
                "data_json": {"room_id": room_id, "room_surface": room_surface, "agent_guard": True},
            }
            save_to_unified_db([guard_payload], source=source)

    def _persist_dayflow_delegation(self, task_description: str) -> None:
        """Write a tagged request message for the dayflow intake pipeline."""
        now_utc = datetime.now(timezone.utc)
        date_seed = now_utc.strftime("%Y-%m-%d")
        request_id = (
            f"dayflow_request:{hashlib.sha256(f'{task_description}|{date_seed}'.encode()).hexdigest()[:16]}"
        )

        payload = {
            "id": request_id,
            "timestamp": now_utc,
            "role": "user",
            "message": task_description,
            "room_id": "dayflow_orchestrator",
            "metadata": {
                "request_id": request_id,
                "request_type": "user_delegation",
                "summary": task_description,
                "created_at": now_utc.isoformat(),
            },
        }

        save_to_unified_db([payload], source="dayflow_request")
