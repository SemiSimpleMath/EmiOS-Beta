"""
Post-node that applies the editor agent's search/replace edits to the spec markdown.

Runs after task_spec::editor in the manager flow. Reads the editor's output
from the blackboard, applies edits, saves the updated spec, and advances
the watermark.
"""
from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.task_utils.task_spec_model import TaskSpec
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.search_replace_editor import apply_edits

logger = get_logger(__name__)

_WATERMARK_KEY = "task_spec_editor_watermark_msg_id"


class TaskSpecEditApplyNode(ControlNode):

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        current_markdown = str(self.blackboard.get_state_value("spec", "") or "").strip()
        if not current_markdown:
            logger.warning("[%s] No spec on blackboard; skipping edit apply.", self.name)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Read the editor agent's output from the blackboard.
        no_changes = bool(self.blackboard.get_state_value("no_changes", True))
        edits = self.blackboard.get_state_value("edits", []) or []

        if no_changes or not edits:
            logger.info("[%s] Editor returned no_op or no edits.", self.name)
            self._advance_watermark()
            self.blackboard.update_state_value("last_agent", self.name)
            return

        edit_result = apply_edits(current_markdown, edits)

        if edit_result.failed > 0:
            logger.warning(
                "[%s] %d edit(s) failed: %s",
                self.name, edit_result.failed, edit_result.failures,
            )

        if edit_result.applied > 0:
            updated_markdown = edit_result.text
            self._save_markdown_and_emit(updated_markdown)
            logger.info("[%s] Applied %d edit(s) to spec.", self.name, edit_result.applied)

        self._advance_watermark()
        self.blackboard.update_state_value("last_agent", self.name)

    # --- Watermark ---

    def _advance_watermark(self) -> None:
        recent = str(self.blackboard.get_state_value("recent_history", "") or "").strip()
        if not recent:
            return
        line_count = len(recent.splitlines())
        self.blackboard.update_state_value(_WATERMARK_KEY, line_count)
        # Persist watermark to unified_log.
        task_id = str(self.blackboard.get_state_value("task_creation_session_id", "") or "").strip()
        if task_id:
            from app.assistant.lib.task_utils.task_spec_store import save_task_spec_draft
            spec = str(self.blackboard.get_state_value("spec", "") or "").strip()
            if spec:
                save_task_spec_draft(task_id, spec_markdown=spec, watermark=line_count, caller="advance_watermark")

    def _save_markdown_and_emit(self, markdown: str) -> None:
        self.blackboard.update_state_value("spec", markdown)

        task_id = str(self.blackboard.get_state_value("task_creation_session_id", "") or "").strip()
        if not task_id:
            task_id = self._extract_task_id_from_markdown(markdown)

        # Persist to unified_log.
        if task_id:
            from app.assistant.lib.task_utils.task_spec_store import save_task_spec_draft
            save_task_spec_draft(task_id, spec_markdown=markdown, caller="task_spec_edit_apply_node")

        # Write back to disk so the spec file stays current.
        if task_id:
            self._write_spec_to_disk(task_id, markdown)

        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        if room_id:
            self._emit_draft_update(room_id=room_id, spec_markdown=markdown)

    @staticmethod
    def _extract_task_id_from_markdown(markdown: str) -> str:
        import re
        lines = markdown.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                title = stripped[2:].strip()
                if title and title not in ("[Untitled Task]", "[New Task]"):
                    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
            if stripped.lower() == "## title" and i + 1 < len(lines):
                title = lines[i + 1].strip()
                if title and title not in ("[New Task]", "[TBD]"):
                    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        return ""

    def _write_spec_to_disk(self, task_id: str, markdown: str) -> None:
        try:
            from app.assistant.utils.path_utils import get_repo_root
            from pathlib import Path

            spec_path = Path(get_repo_root()) / "tasks" / task_id / "task_spec.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(markdown, encoding="utf-8")
            logger.info("[%s] Wrote spec to disk: %s", self.name, spec_path)
        except Exception as e:
            logger.error("[%s] Failed writing spec to disk: %s", self.name, e)
            logger.debug("[%s] disk write exception", self.name, exc_info=True)

    def _emit_draft_update(self, *, room_id: str, spec_markdown: str) -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            from app.assistant.utils.identity_names import get_required_assistant_name

            assistant_name = get_required_assistant_name()
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(
                    widget_data=[{
                        "data_type": "task_draft_update",
                        "spec_markdown": spec_markdown,
                    }],
                ),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
        except Exception as e:
            logger.error("[%s] Failed emitting draft update: %s", self.name, e)
            logger.debug("[%s] draft update emit exception", self.name, exc_info=True)
