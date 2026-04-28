from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class DocCreationSessionService:
    """
    Manages doc-creation mode sessions keyed by room.

    Sessions are stored in the global blackboard under a namespaced key so they
    survive across multiple turns within the same room conversation.
    """

    MODE_NAME = "doc_creation_mode"
    SESSIONS_KEY = "doc_creation_sessions"
    INDEX_BY_ROOM_KEY = "doc_creation_sessions_by_room"

    VALID_DOC_TYPES = {"md", "gdoc"}

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
            raise ValueError("room_id is required for doc creation binding key")
        if not surf:
            raise ValueError("surface is required for doc creation binding key")
        return f"{surf}::{rid}::{ctx}"

    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        raw = self._blackboard.get_state_value(self.SESSIONS_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid doc creation sessions state type: %s", type(raw))
            raise RuntimeError("Invalid doc_creation_sessions state")
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
            logger.error("Invalid doc creation room index type: %s", type(raw))
            raise RuntimeError("Invalid doc_creation_sessions_by_room state")
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

    def activate_room_binding(
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

        # Derive a human-readable filename: user-supplied name, else date-based fallback
        safe_name = self._sanitize_doc_name(doc_name)
        if not safe_name:
            safe_name = f"doc-{date.today().isoformat()}"

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
        session_id = f"doc_{uuid.uuid4().hex[:12]}"
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "status": "active",
            "room_mode": self.MODE_NAME,
            "doc_type": doc_type,
            "doc_name": safe_name,
            "room_id": room_id,
            "room_surface": surface,
            "room_context_id": context_id,
            "room_key": room_key,
            "created_at_utc": now_iso,
            "updated_at_utc": now_iso,
            "initiated_by": initiated_by,
            "initial_prompt": initial_prompt,
            "doc_markdown": "",
            "doc_id": None,
        }
        sessions[session_id] = payload
        room_index[room_key] = session_id
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        logger.debug("Doc creation session activated: %s doc_type=%s room_key=%s", session_id, doc_type, room_key)

        # Seed unified_log doc_store so the new session has a content row from turn 1.
        try:
            from app.assistant.lib.doc_utils.doc_store import load_doc_draft, create_doc_draft
            if load_doc_draft(session_id) is None:
                create_doc_draft(
                    doc_id=session_id,
                    doc_type=doc_type,
                    doc_name=safe_name,
                    caller="DocCreationSessionService.activate_room_binding",
                )
        except Exception as e:
            logger.error("Failed seeding doc_store for session %s: %s", session_id, e)
            logger.debug("doc_store seed exception", exc_info=True)

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
        logger.debug("Doc creation session deactivated: %s reason=%s", session_id, reason)
        return True
