from __future__ import annotations

from typing import Any, Dict

from app.assistant.room_session_manager.services.room_binding_session_service import (
    RoomBindingSessionService,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class DocCreationSessionService(RoomBindingSessionService):
    """
    Manages doc-creation mode sessions keyed by room.

    Sessions are stored in the global blackboard under a namespaced key so they
    survive across multiple turns within the same room conversation.
    """

    MODE_NAME = "doc_creation_mode"
    SESSIONS_KEY = "doc_creation_sessions"
    INDEX_BY_ROOM_KEY = "doc_creation_sessions_by_room"
    SESSION_ID_PREFIX = "doc"
    LABEL = "doc creation"

    VALID_DOC_TYPES = {"md", "gdoc"}

    @staticmethod
    def _sanitize_doc_name(raw: str) -> str:
        """Convert a user-supplied name into a safe filename stem."""
        import re
        stem = str(raw or "").strip()
        # Replace whitespace runs with hyphens, strip characters unsafe in filenames
        stem = re.sub(r"\s+", "-", stem)
        stem = re.sub(r"[^\w\-]", "", stem, flags=re.ASCII)
        stem = stem.strip("-_").lower()
        return stem or ""

    def activate_room_binding(  # type: ignore[override]
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
        initiated_by: str,
        doc_type: str = "md",
        doc_name: str = "",
        initial_prompt: str = "",
    ) -> dict[str, Any]:
        from datetime import date
        doc_type = str(doc_type or "md").strip().lower()
        if doc_type not in self.VALID_DOC_TYPES:
            raise ValueError(f"Invalid doc_type {doc_type!r}. Must be one of: {self.VALID_DOC_TYPES}")

        # Human-readable filename: user-supplied name, else a date-based default.
        safe_name = self._sanitize_doc_name(doc_name)
        if not safe_name:
            safe_name = f"doc-{date.today().isoformat()}"

        return super().activate_room_binding(
            room_id=room_id,
            surface=surface,
            context_id=context_id,
            initiated_by=initiated_by,
            extra_fields={
                "doc_type": doc_type,
                "doc_name": safe_name,
                "initial_prompt": initial_prompt,
                "doc_markdown": "",
                "doc_id": None,
            },
        )

    def _on_activated(self, payload: Dict[str, Any]) -> None:
        # Seed unified_log doc_store so the new session has a content row from turn 1.
        session_id = str(payload.get("session_id") or "")
        try:
            from app.assistant.lib.doc_utils.doc_store import load_doc_draft, create_doc_draft
            if load_doc_draft(session_id) is None:
                create_doc_draft(
                    doc_id=session_id,
                    doc_type=str(payload.get("doc_type") or "md"),
                    doc_name=str(payload.get("doc_name") or ""),
                    caller="DocCreationSessionService.activate_room_binding",
                )
        except Exception as e:
            logger.error("Failed seeding doc_store for session %s: %s", session_id, e)
            logger.debug("doc_store seed exception", exc_info=True)

    def update_doc_markdown(self, *, session_id: str, doc_markdown: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Doc creation session not found: {session_id}")
        payload["doc_markdown"] = str(doc_markdown or "")
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def set_doc_id(self, *, session_id: str, doc_id: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Doc creation session not found: {session_id}")
        payload["doc_id"] = str(doc_id or "").strip()
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)
