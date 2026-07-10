from __future__ import annotations

from typing import Any, Dict

from app.assistant.room_session_manager.services.room_binding_session_service import (
    RoomBindingSessionService,
)
from app.assistant.room_session_manager.services._session_state_lock import with_session_state_lock
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PlanSessionService(RoomBindingSessionService):
    MODE_NAME = "planning_mode"
    SESSIONS_KEY = "room_mode_sessions"
    INDEX_BY_ROOM_KEY = "room_mode_sessions_by_room"
    SESSION_ID_PREFIX = "plan"
    SESSION_ID_FIELD = "plan_session_id"
    LABEL = "plan"

    PLAN_INDEX_BY_TICKET_KEY = "room_mode_sessions_by_ticket"

    def _load_ticket_index(self) -> Dict[str, str]:
        raw = self._blackboard.get_state_value(self.PLAN_INDEX_BY_TICKET_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid plan ticket index type: %s", type(raw))
            raise RuntimeError("Invalid room_mode_sessions_by_ticket state")
        out: Dict[str, str] = {}
        for ticket_id, session_id in raw.items():
            if (
                isinstance(ticket_id, str)
                and ticket_id.strip()
                and isinstance(session_id, str)
                and session_id.strip()
            ):
                out[ticket_id] = session_id
        return out

    def _save_ticket_index(self, index: Dict[str, str]) -> None:
        if not isinstance(index, dict):
            raise TypeError("ticket index must be a dict")
        self._blackboard.update_state_value(self.PLAN_INDEX_BY_TICKET_KEY, index)

    def _on_deactivated(self, payload: Dict[str, Any]) -> None:
        ticket_id = str(payload.get("ticket_id") or "").strip()
        if not ticket_id:
            return
        ticket_index = self._load_ticket_index()
        ticket_index.pop(ticket_id, None)
        self._save_ticket_index(ticket_index)

    @with_session_state_lock
    def get_active_ticket_session_for_ticket(self, ticket_id: str) -> Dict[str, Any] | None:
        ticket_key = str(ticket_id or "").strip()
        if not ticket_key:
            return None
        index = self._load_ticket_index()
        sessions = self._load_sessions()
        sid = index.get(ticket_key)
        if not isinstance(sid, str) or not sid.strip():
            return None
        session = sessions.get(sid)
        if not self._is_active(session):
            return None
        return {"session_id": sid, **session}

    @with_session_state_lock
    def list_active_ticket_ids(self) -> set[str]:
        active_ticket_ids: set[str] = set()
        index = self._load_ticket_index()
        sessions = self._load_sessions()
        for ticket_id, plan_session_id in index.items():
            session = sessions.get(plan_session_id)
            if self._is_active(session):
                active_ticket_ids.add(ticket_id)
        return active_ticket_ids

    @with_session_state_lock
    def start_or_resume_ticket_session(
        self,
        *,
        ticket_id: str,
        room_id: str,
        room_context_id: str,
        room_surface: str = "ui",
    ) -> Dict[str, Any]:
        ticket_key = str(ticket_id or "").strip()
        if not ticket_key:
            raise ValueError("ticket_id is required")
        existing = self.get_active_ticket_session_for_ticket(ticket_key)
        if isinstance(existing, dict):
            return {
                "ticket_id": ticket_key,
                "plan_session_id": existing["session_id"],
                "status": "active",
                "room_id": str(existing.get("room_id") or room_id),
                "room_context_id": str(existing.get("room_context_id") or room_context_id),
            }
        session = self.activate_room_binding(
            room_id=room_id,
            surface=room_surface,
            context_id=room_context_id,
            initiated_by=f"ticket::{ticket_key}",
        )
        plan_session_id = str(session.get("plan_session_id") or "").strip()
        if not plan_session_id:
            raise RuntimeError("activate_room_binding returned missing plan_session_id")
        sessions = self._load_sessions()
        ticket_index = self._load_ticket_index()
        payload = sessions.get(plan_session_id)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Plan session not found after activation: {plan_session_id}")
        payload["ticket_id"] = ticket_key
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[plan_session_id] = payload
        ticket_index[ticket_key] = plan_session_id
        self._save_sessions(sessions)
        self._save_ticket_index(ticket_index)
        return {
            "ticket_id": ticket_key,
            "plan_session_id": plan_session_id,
            "status": "active",
            "room_id": room_id,
            "room_context_id": room_context_id,
        }

    def get_ticket_session(self, plan_session_id: str) -> Dict[str, Any] | None:
        sessions = self._load_sessions()
        payload = sessions.get(plan_session_id)
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    @with_session_state_lock
    def touch_ticket_session(self, plan_session_id: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(plan_session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Plan session not found: {plan_session_id}")
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[plan_session_id] = payload
        self._save_sessions(sessions)

    @with_session_state_lock
    def set_ticket_session_status(
        self,
        *,
        ticket_id: str,
        plan_session_id: str,
        status: str,
    ) -> None:
        if status not in {"active", "done", "cancelled"}:
            raise ValueError(f"Invalid plan session status: {status}")
        sessions = self._load_sessions()
        room_index = self._load_room_index()
        ticket_index = self._load_ticket_index()
        payload = sessions.get(plan_session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Plan session not found: {plan_session_id}")
        if str(payload.get("ticket_id") or "") != ticket_id:
            raise ValueError("Plan session ticket mismatch")
        payload["status"] = status
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[plan_session_id] = payload
        room_key = str(payload.get("room_key") or "").strip()
        if status == "active":
            ticket_index[ticket_id] = plan_session_id
            if room_key:
                room_index[room_key] = plan_session_id
        else:
            ticket_index.pop(ticket_id, None)
            if room_key:
                room_index.pop(room_key, None)
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        self._save_ticket_index(ticket_index)
