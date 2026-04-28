"""
Pre-node for the doc writer agent.

Gates the doc_writer: only runs it when the chat agent sets update_doc_tf=True.
Prepares context by assembling recent exchanges since the last watermark.
Same pattern as TaskSpecRouterNode but for document editing.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_DOC_WRITER_AGENT = "doc_editor::editor"
_SKIP_TARGET = "doc_create_final_router_node"
_WATERMARK_KEY = "doc_writer_watermark_line_idx"


class DocWriterPreNode(ControlNode):

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        # Ensure doc content is on local blackboard from global.
        if not str(self.blackboard.get_state_value("doc_markdown_last_emitted", "") or "").strip():
            from app.assistant.ServiceLocator.service_locator import DI
            global_doc = str(DI.global_blackboard.get_state_value("doc_markdown_last_emitted", "") or "").strip()
            if global_doc:
                self.blackboard.update_state_value("doc_markdown_last_emitted", global_doc)

        update_doc = bool(self.blackboard.get_state_value("update_doc_tf", False))
        self.blackboard.update_state_value("update_doc_tf", False)

        if not update_doc:
            # Skip writer — go straight to final router.
            self.blackboard.update_state_value("next_agent", _SKIP_TARGET)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Build the exchanges since the watermark.
        exchanges = self._get_exchanges_since_watermark()

        if not exchanges:
            logger.info("[%s] update_doc_tf set but no exchanges since watermark.", self.name)
            self.blackboard.update_state_value("next_agent", _SKIP_TARGET)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Prepare context for the writer.
        self.blackboard.update_state_value("recent_exchanges", exchanges)
        self.blackboard.update_state_value("next_agent", _DOC_WRITER_AGENT)
        self.blackboard.update_state_value("last_agent", self.name)

    def _get_exchanges_since_watermark(self) -> str:
        watermark_idx = int(self.blackboard.get_state_value(_WATERMARK_KEY, 0) or 0)

        # Try to restore watermark from doc_store if not on blackboard.
        if watermark_idx == 0:
            session_id = str(self.blackboard.get_state_value("doc_creation_session_id", "") or "").strip()
            if session_id:
                try:
                    from app.assistant.lib.doc_utils.doc_store import load_doc_draft
                    draft = load_doc_draft(session_id)
                    if draft is not None:
                        watermark_idx = int(draft.get("watermark", 0) or 0)
                except Exception:
                    pass

        # Build from blackboard state.
        task = str(self.blackboard.get_state_value("task", "") or "").strip()
        incoming = str(self.blackboard.get_state_value("incoming_message", "") or "").strip()
        chat_response = str(self.blackboard.get_state_value("chat_response", "") or "").strip()
        recent_history = str(self.blackboard.get_state_value("recent_history", "") or "").strip()

        if recent_history:
            recent = recent_history
        else:
            parts = []
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
