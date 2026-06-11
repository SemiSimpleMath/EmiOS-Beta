from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.assistant.control_nodes.chat_task_router_node import ChatTaskRouterNode
from app.assistant.message_manager.save_to_unified_db import save_to_unified_db
from app.assistant.utils.logging_config import get_logger

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
