"""
Document editor room.

Manages a document (markdown or Google Doc) through conversational editing.
Draft content persists in unified_log via doc_store. Session binding
(routing layer) still goes through DocCreationSessionService.
"""
from __future__ import annotations

from app.assistant.room_handlers.editor_room import EditorRoom
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

BLANK_DOC = "# Untitled Document\n\n"


class DocEditorRoom(EditorRoom):
    """Room for creating and editing documents."""

    def __init__(self, room_id: str = "doc_editor"):
        super().__init__(room_id)

    def _get_session_service(self):
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.room_session_manager.services.doc_creation_session_service import (
            DocCreationSessionService,
        )
        return DocCreationSessionService(blackboard=DI.global_blackboard)

    def ensure_session(self, *, blackboard, surface: str = "ui", context_id: str = "main", room_id: str = "") -> str:
        """Find or create a doc editing session for this room."""
        existing = str(blackboard.get_state_value("doc_creation_session_id", "") or "").strip()
        if existing:
            return existing

        try:
            svc = self._get_session_service()

            binding = svc.get_active_room_binding(
                room_id=self.room_id, surface=surface, context_id=context_id,
            )
            if binding:
                sid = str(binding.get("session_id") or "").strip()
                if sid:
                    blackboard.update_state_value("doc_creation_session_id", sid)
                    return sid

            binding = svc.activate_room_binding(
                room_id=self.room_id, surface=surface, context_id=context_id,
                initiated_by="system",
            )
            sid = str(binding.get("session_id") or "").strip()
            if sid:
                blackboard.update_state_value("doc_creation_session_id", sid)
                # Seed unified_log draft so subsequent turns can load it.
                from app.assistant.lib.doc_utils.doc_store import load_doc_draft, create_doc_draft
                if load_doc_draft(sid) is None:
                    create_doc_draft(
                        doc_id=sid,
                        doc_type=str(binding.get("doc_type") or "md"),
                        doc_name=str(binding.get("doc_name") or ""),
                        caller="doc_editor_room.ensure_session",
                    )
            return sid
        except Exception as e:
            logger.error("[DocEditorRoom] Failed to ensure session: %s", e)
            logger.debug("[DocEditorRoom] ensure session exception", exc_info=True)
            return ""

    def restore_state(self, *, blackboard, session_id: str) -> None:
        """Load the doc from unified_log onto the blackboard.

        Google Doc content is fetched at load time (slash command or explicit
        refresh), not here — during edits we trust unified_log.
        """
        try:
            from app.assistant.lib.doc_utils.doc_store import load_doc_draft

            draft = load_doc_draft(session_id)
            if draft is None:
                # First touch — no doc_store row yet.
                blackboard.update_state_value("doc_markdown", BLANK_DOC)
                blackboard.update_state_value("doc_markdown_last_emitted", BLANK_DOC)
                return

            google_doc_id = str(draft.get("google_doc_id") or "").strip()
            content = str(draft.get("doc_markdown") or "").strip()
            if content:
                blackboard.update_state_value("doc_markdown", content)
                blackboard.update_state_value("doc_markdown_last_emitted", content)
                logger.debug(
                    "[DocEditorRoom] Restored doc (%d chars) from doc_store %s",
                    len(content), session_id,
                )
            else:
                blackboard.update_state_value("doc_markdown", BLANK_DOC)
                blackboard.update_state_value("doc_markdown_last_emitted", BLANK_DOC)

            doc_type = str(draft.get("doc_type") or "md").strip()
            doc_name = str(draft.get("doc_name") or "").strip()
            blackboard.update_state_value("doc_type", doc_type)
            blackboard.update_state_value("doc_name", doc_name)
            if google_doc_id:
                blackboard.update_state_value("doc_id", google_doc_id)
        except Exception as e:
            logger.error("[DocEditorRoom] Failed to restore doc: %s", e)
            logger.debug("[DocEditorRoom] restore doc exception", exc_info=True)

    def persist_state(self, *, blackboard, session_id: str) -> None:
        """Save the current doc from the blackboard to doc_store."""
        try:
            draft = str(blackboard.get_state_value("doc_markdown", "") or "").strip()
            if not draft:
                return
            from app.assistant.lib.doc_utils.doc_store import save_doc_draft
            save_doc_draft(
                session_id,
                doc_markdown=draft,
                caller="doc_editor_room.persist_state",
            )
        except Exception as e:
            logger.error("[DocEditorRoom] Failed to persist doc: %s", e)
            logger.debug("[DocEditorRoom] persist doc exception", exc_info=True)

    def finalize_turn(self, *, blackboard) -> None:
        """Post-turn: persist state to doc_store."""
        session_id = str(blackboard.get_state_value("doc_creation_session_id", "") or "").strip()
        if session_id:
            self.persist_state(blackboard=blackboard, session_id=session_id)
