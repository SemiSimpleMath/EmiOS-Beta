"""
Control node that routes to the spec editor agent when updates are needed.

Flow:
1. Chat agent sets update_spec_tf=True when the conversation has content
   that should update the spec.
2. This node prepares context (current spec + exchanges) on the blackboard
   and routes to the editor agent.
3. If no update needed, passes through to the next node in the state map.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_EDITOR_AGENT = "task_spec::editor"
_WATERMARK_KEY = "task_spec_editor_watermark_msg_id"


class TaskSpecRouterNode(ControlNode):

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        # Extract task_id from room_id (task_spec::spammer_finder → spammer_finder).
        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        if "::" in room_id and not self.blackboard.get_state_value("task_creation_session_id"):
            task_id = room_id.split("::", 1)[1]
            self.blackboard.update_state_value("task_creation_session_id", task_id)

        # Always load the spec from unified_log onto the local blackboard.
        spec = self._load_spec_markdown()
        self.blackboard.update_state_value("spec", spec)

        update_spec = bool(self.blackboard.get_state_value("update_spec_tf", False))
        self.blackboard.update_state_value("update_spec_tf", False)

        if not update_spec:
            # Skip editor — go straight to the final router.
            self.blackboard.update_state_value("next_agent", "task_spec_edit_apply_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Prepare exchanges for the editor.
        exchanges = self._get_exchanges_since_watermark()

        if not exchanges:
            logger.info("[%s] update_spec_tf set but no exchanges since watermark.", self.name)
            self.blackboard.update_state_value("next_agent", "task_spec_edit_apply_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        self.blackboard.update_state_value("recent_exchanges", exchanges)

        # Route to the editor agent — the manager loop will call it.
        self.blackboard.update_state_value("next_agent", _EDITOR_AGENT)
        self.blackboard.update_state_value("last_agent", self.name)

    # --- Exchange retrieval ---

    def _get_exchanges_since_watermark(self) -> str:
        watermark_idx = int(self.blackboard.get_state_value(_WATERMARK_KEY, 0) or 0)
        if watermark_idx == 0:
            task_id = str(self.blackboard.get_state_value("task_creation_session_id", "") or "").strip()
            if task_id:
                from app.assistant.lib.task_utils.task_spec_store import load_task_spec_draft
                draft = load_task_spec_draft(task_id)
                if draft:
                    watermark_idx = int(draft.get("editor_watermark", 0) or 0)

        parts = []
        task = str(self.blackboard.get_state_value("task", "") or "").strip()
        incoming = str(self.blackboard.get_state_value("incoming_message", "") or "").strip()
        chat_response = str(self.blackboard.get_state_value("chat_response", "") or "").strip()
        recent_history = str(self.blackboard.get_state_value("recent_history", "") or "").strip()

        if recent_history:
            recent = recent_history
        else:
            user_msg = task or incoming
            if user_msg:
                parts.append(f"User: {user_msg}")
            if chat_response:
                parts.append(f"Assistant: {chat_response}")
            recent = "\n".join(parts)

        if not recent:
            return ""

        lines = recent.splitlines()
        if watermark_idx > 0 and watermark_idx < len(lines):
            lines = lines[watermark_idx:]

        return "\n".join(lines).strip()

    # --- Spec loading ---

    def _get_session_service(self):
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.room_session_manager.services.task_creation_session_service import (
            TaskCreationSessionService,
        )
        return TaskCreationSessionService(blackboard=DI.global_blackboard)

    def _load_spec_markdown(self) -> str:
        # Local blackboard first (already loaded this cycle).
        md = str(self.blackboard.get_state_value("spec", "") or "").strip()
        if md:
            return md

        # Load from unified_log — the canonical persistent store.
        task_id = str(self.blackboard.get_state_value("task_creation_session_id", "") or "").strip()
        if task_id:
            from app.assistant.lib.task_utils.task_spec_store import load_task_spec_draft
            draft = load_task_spec_draft(task_id)
            if draft and draft.get("spec_markdown"):
                return draft["spec_markdown"]

        return "## Title\n[New Task]\n\n## Description\n[TBD]\n\n## Steps\n[No steps defined yet]\n\n## Completion\n[TBD]"
