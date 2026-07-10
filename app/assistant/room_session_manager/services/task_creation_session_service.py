from __future__ import annotations

from typing import Any, Dict

from app.assistant.room_session_manager.services.room_binding_session_service import (
    RoomBindingSessionService,
)
from app.assistant.room_session_manager.services._session_state_lock import with_session_state_lock
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TaskCreationSessionService(RoomBindingSessionService):
    """
    Manages task-creation mode sessions keyed by room.

    Sessions are stored in the global blackboard under a namespaced key so they
    survive across multiple turns within the same room conversation.
    """

    MODE_NAME = "task_creation_mode"
    SESSIONS_KEY = "task_creation_sessions"
    INDEX_BY_ROOM_KEY = "task_creation_sessions_by_room"
    SESSION_ID_PREFIX = "task"
    LABEL = "task creation"

    def activate_room_binding(  # type: ignore[override]
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
        initiated_by: str,
        initial_prompt: str = "",
    ) -> dict[str, Any]:
        return super().activate_room_binding(
            room_id=room_id,
            surface=surface,
            context_id=context_id,
            initiated_by=initiated_by,
            extra_fields={
                "initial_prompt": initial_prompt,
                "draft_spec": "",
                "compiled_task_id": None,
            },
        )

    @with_session_state_lock
    def update_draft_spec(self, *, session_id: str, draft_spec: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Task creation session not found: {session_id}")
        payload["draft_spec"] = str(draft_spec or "")
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    @with_session_state_lock
    def update_task_spec_object(self, *, session_id: str, spec_dict: dict) -> None:
        """Persist the structured TaskSpec dict across turns."""
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Task creation session not found: {session_id}")
        payload["task_spec_object"] = spec_dict
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def get_task_spec_object(self, *, session_id: str) -> dict | None:
        """Load the persisted TaskSpec dict."""
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            return None
        return payload.get("task_spec_object")

    @with_session_state_lock
    def update_task_id(self, *, session_id: str, task_id: str) -> None:
        """Set or update the task_id (folder name) for this session."""
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Task creation session not found: {session_id}")
        payload["task_id"] = str(task_id or "").strip()
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def get_task_id(self, *, session_id: str) -> str:
        """Get the task_id for this session."""
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("task_id") or "").strip()

    @with_session_state_lock
    def set_compiled_task_id(self, *, session_id: str, compiled_task_id: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Task creation session not found: {session_id}")
        payload["compiled_task_id"] = str(compiled_task_id or "").strip()
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)
