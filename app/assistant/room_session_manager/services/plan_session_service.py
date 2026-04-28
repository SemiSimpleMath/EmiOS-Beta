from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PlanSessionService:
    MODE_NAME = "planning_mode"
    PLAN_SESSIONS_KEY = "room_mode_sessions"
    PLAN_INDEX_BY_ROOM_KEY = "room_mode_sessions_by_room"
    PLAN_INDEX_BY_TICKET_KEY = "room_mode_sessions_by_ticket"

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
            raise ValueError("room_id is required for plan binding key")
        if not surf:
            raise ValueError("surface is required for plan binding key")
        return f"{surf}::{rid}::{ctx}"

    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        raw = self._blackboard.get_state_value(self.PLAN_SESSIONS_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid plan sessions state type: %s", type(raw))
            raise RuntimeError("Invalid room_mode_sessions state")
        out: Dict[str, Dict[str, Any]] = {}
        for sid, payload in raw.items():
            if isinstance(sid, str) and sid.strip() and isinstance(payload, dict):
                out[sid] = dict(payload)
        return out

    def _save_sessions(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        if not isinstance(sessions, dict):
            raise TypeError("sessions must be a dict")
        self._blackboard.update_state_value(self.PLAN_SESSIONS_KEY, sessions)

    def _load_room_index(self) -> Dict[str, str]:
        raw = self._blackboard.get_state_value(self.PLAN_INDEX_BY_ROOM_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid plan room index type: %s", type(raw))
            raise RuntimeError("Invalid room_mode_sessions_by_room state")
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
        self._blackboard.update_state_value(self.PLAN_INDEX_BY_ROOM_KEY, index)

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
    ) -> dict[str, Any]:
        room_key = self.room_binding_key(room_id=room_id, surface=surface, context_id=context_id)
        now_iso = self._utc_now_iso()
        sessions = self._load_sessions()
        room_index = self._load_room_index()
        session_id = room_index.get(room_key)
        existing = sessions.get(session_id or "")
        if self._is_active(existing):
            existing["updated_at_utc"] = now_iso
            sessions[str(existing.get("plan_session_id") or session_id)] = existing
            self._save_sessions(sessions)
            return dict(existing)
        session_id = f"plan_{uuid.uuid4().hex[:12]}"
        payload = {
            "plan_session_id": session_id,
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
        sessions[session_id] = payload
        room_index[room_key] = session_id
        self._save_sessions(sessions)
        self._save_room_index(room_index)
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
        ticket_index = self._load_ticket_index()
        session_id = room_index.get(room_key)
        payload = sessions.get(session_id or "")
        if not isinstance(payload, dict):
            return False
        payload["status"] = "closed"
        payload["updated_at_utc"] = now_iso
        payload["closed_reason"] = reason
        sessions[str(payload.get("plan_session_id") or session_id)] = payload
        room_index.pop(room_key, None)
        ticket_id = str(payload.get("ticket_id") or "").strip()
        if ticket_id:
            ticket_index.pop(ticket_id, None)
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        self._save_ticket_index(ticket_index)
        return True

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

    def list_active_ticket_ids(self) -> set[str]:
        active_ticket_ids: set[str] = set()
        index = self._load_ticket_index()
        sessions = self._load_sessions()
        for ticket_id, plan_session_id in index.items():
            session = sessions.get(plan_session_id)
            if self._is_active(session):
                active_ticket_ids.add(ticket_id)
        return active_ticket_ids

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

    def touch_ticket_session(self, plan_session_id: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(plan_session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Plan session not found: {plan_session_id}")
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[plan_session_id] = payload
        self._save_sessions(sessions)

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
