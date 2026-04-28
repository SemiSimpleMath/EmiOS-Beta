from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TaskCreationSessionService:
    """
    Manages task-creation mode sessions keyed by room.

    Sessions are stored in the global blackboard under a namespaced key so they
    survive across multiple turns within the same room conversation.
    """

    MODE_NAME = "task_creation_mode"
    SESSIONS_KEY = "task_creation_sessions"
    INDEX_BY_ROOM_KEY = "task_creation_sessions_by_room"

    def __init__(self, *, blackboard: GlobalBlackBoard) -> None:
        self._blackboard = blackboard

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def room_binding_key(self, *, room_id: str, surface: str, context_id: str) -> str:
        rid = str(room_id or "").strip()
        surf = str(surface or "").strip().lower()
        ctx = str(context_id or "main").strip() or "main"
        if not rid:
            raise ValueError("room_id is required for task creation binding key")
        if not surf:
            raise ValueError("surface is required for task creation binding key")
        return f"{surf}::{rid}::{ctx}"

    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        raw = self._blackboard.get_state_value(self.SESSIONS_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid task creation sessions state type: %s", type(raw))
            raise RuntimeError("Invalid task_creation_sessions state")
        out: Dict[str, Dict[str, Any]] = {}
        for sid, payload in raw.items():
            if isinstance(sid, str) and sid.strip() and isinstance(payload, dict):
                out[sid] = dict(payload)
        return out

    def _save_sessions(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        if not isinstance(sessions, dict):
            raise TypeError("sessions must be a dict")
        self._blackboard.update_state_value(self.SESSIONS_KEY, sessions)

    def _load_room_index(self) -> Dict[str, str]:
        raw = self._blackboard.get_state_value(self.INDEX_BY_ROOM_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid task creation room index type: %s", type(raw))
            raise RuntimeError("Invalid task_creation_sessions_by_room state")
        out: Dict[str, str] = {}
        for room_key, session_id in raw.items():
            if (
                isinstance(room_key, str)
                and room_key.strip()
                and isinstance(session_id, str)
                and session_id.strip()
            ):
                out[room_key] = session_id
        return out

    def _save_room_index(self, index: Dict[str, str]) -> None:
        if not isinstance(index, dict):
            raise TypeError("room index must be a dict")
        self._blackboard.update_state_value(self.INDEX_BY_ROOM_KEY, index)

    @staticmethod
    def _is_active(payload: Dict[str, Any] | None) -> bool:
        return isinstance(payload, dict) and str(payload.get("status") or "").strip() == "active"

    def activate_room_binding(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
        initiated_by: str,
        initial_prompt: str = "",
    ) -> dict[str, Any]:
        room_key = self.room_binding_key(room_id=room_id, surface=surface, context_id=context_id)
        now_iso = self._utc_now_iso()
        sessions = self._load_sessions()
        room_index = self._load_room_index()
        session_id = room_index.get(room_key)
        existing = sessions.get(session_id or "")
        if self._is_active(existing):
            existing["updated_at_utc"] = now_iso
            sessions[str(existing.get("session_id") or session_id)] = existing
            self._save_sessions(sessions)
            return dict(existing)
        session_id = f"task_{uuid.uuid4().hex[:12]}"
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "status": "active",
            "room_mode": self.MODE_NAME,
            "room_id": room_id,
            "room_surface": surface,
            "room_context_id": context_id,
            "room_key": room_key,
            "created_at_utc": now_iso,
            "updated_at_utc": now_iso,
            "initiated_by": initiated_by,
            "initial_prompt": initial_prompt,
            "draft_spec": "",
            "compiled_task_id": None,
        }
        sessions[session_id] = payload
        room_index[room_key] = session_id
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        logger.debug("Task creation session activated: %s room_key=%s", session_id, room_key)
        return dict(payload)

    def get_active_room_binding(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
    ) -> dict[str, Any] | None:
        room_key = self.room_binding_key(room_id=room_id, surface=surface, context_id=context_id)
        sessions = self._load_sessions()
        room_index = self._load_room_index()
        session_id = room_index.get(room_key)
        payload = sessions.get(session_id or "")
        if not self._is_active(payload):
            return None
        return dict(payload)

    def update_draft_spec(self, *, session_id: str, draft_spec: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Task creation session not found: {session_id}")
        payload["draft_spec"] = str(draft_spec or "")
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

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

    def set_compiled_task_id(self, *, session_id: str, compiled_task_id: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Task creation session not found: {session_id}")
        payload["compiled_task_id"] = str(compiled_task_id or "").strip()
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def deactivate_room_binding(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
        reason: str,
    ) -> bool:
        room_key = self.room_binding_key(room_id=room_id, surface=surface, context_id=context_id)
        now_iso = self._utc_now_iso()
        sessions = self._load_sessions()
        room_index = self._load_room_index()
        session_id = room_index.get(room_key)
        payload = sessions.get(session_id or "")
        if not isinstance(payload, dict):
            return False
        payload["status"] = "closed"
        payload["updated_at_utc"] = now_iso
        payload["closed_reason"] = reason
        sessions[str(payload.get("session_id") or session_id)] = payload
        room_index.pop(room_key, None)
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        logger.debug("Task creation session deactivated: %s reason=%s", session_id, reason)
        return True
