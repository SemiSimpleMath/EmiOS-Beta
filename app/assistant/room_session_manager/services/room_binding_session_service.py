"""Generic room-bound mode-session store (dedup audit 2026-06-10).

The task-creation / doc-creation / plan / geoguessr session services all
manage the same thing: blackboard-persisted sessions keyed by a
``surface::room::context`` binding, with an active-session-per-room index
and an activate / get / deactivate lifecycle. The four copies differed
only in state-key names, the session-id prefix/field, the mode-specific
initial payload, and per-mode accessor methods — so the spine lives here
and subclasses declare those knobs.

Hooks:
- ``_initial_payload_extras()``  — static per-mode payload fields
- ``_on_activated(payload)``     — fires once per NEWLY created session
- ``_on_deactivated(payload)``   — fires before the deactivate saves
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.room_session_manager.services._session_state_lock import with_session_state_lock
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class RoomBindingSessionService:
    MODE_NAME: str = ""
    SESSIONS_KEY: str = ""
    INDEX_BY_ROOM_KEY: str = ""
    SESSION_ID_PREFIX: str = "session"
    SESSION_ID_FIELD: str = "session_id"
    # Human label used in error/log messages ("task creation", "geoguessr", …)
    LABEL: str = "room mode"

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
            raise ValueError(f"room_id is required for {self.LABEL} binding key")
        if not surf:
            raise ValueError(f"surface is required for {self.LABEL} binding key")
        return f"{surf}::{rid}::{ctx}"

    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        raw = self._blackboard.get_state_value(self.SESSIONS_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid %s sessions state type: %s", self.LABEL, type(raw))
            raise RuntimeError(f"Invalid {self.SESSIONS_KEY} state")
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
            logger.error("Invalid %s room index type: %s", self.LABEL, type(raw))
            raise RuntimeError(f"Invalid {self.INDEX_BY_ROOM_KEY} state")
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

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def _initial_payload_extras(self) -> Dict[str, Any]:
        return {}

    def _on_activated(self, payload: Dict[str, Any]) -> None:
        """Called once for a NEWLY created session (not on resume)."""

    def _on_deactivated(self, payload: Dict[str, Any]) -> None:
        """Called after a session is marked closed, before indexes save."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @with_session_state_lock
    def activate_room_binding(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
        initiated_by: str,
        extra_fields: Dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        room_key = self.room_binding_key(room_id=room_id, surface=surface, context_id=context_id)
        now_iso = self._utc_now_iso()
        sessions = self._load_sessions()
        room_index = self._load_room_index()
        session_id = room_index.get(room_key)
        existing = sessions.get(session_id or "")
        if self._is_active(existing):
            existing["updated_at_utc"] = now_iso
            sessions[str(existing.get(self.SESSION_ID_FIELD) or session_id)] = existing
            self._save_sessions(sessions)
            return dict(existing)
        session_id = f"{self.SESSION_ID_PREFIX}_{uuid.uuid4().hex[:12]}"
        payload: Dict[str, Any] = {
            self.SESSION_ID_FIELD: session_id,
            "status": "active",
            "room_mode": self.MODE_NAME,
            "room_id": room_id,
            "room_surface": surface,
            "room_context_id": context_id,
            "room_key": room_key,
            "created_at_utc": now_iso,
            "updated_at_utc": now_iso,
            "initiated_by": initiated_by,
        }
        payload.update(self._initial_payload_extras())
        if extra_fields:
            payload.update(extra_fields)
        sessions[session_id] = payload
        room_index[room_key] = session_id
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        logger.debug("%s session activated: %s room_key=%s", self.LABEL, session_id, room_key)
        self._on_activated(payload)
        return dict(payload)

    @with_session_state_lock
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

    @with_session_state_lock
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
        sessions[str(payload.get(self.SESSION_ID_FIELD) or session_id)] = payload
        room_index.pop(room_key, None)
        self._on_deactivated(payload)
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        logger.debug("%s session deactivated: %s reason=%s", self.LABEL, session_id, reason)
        return True
