from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class GeoguessrSessionService:
    """
    Manages active GeoGuessr game sessions keyed by room.

    Stores per-session state:
      - screenshot_paths: ordered list of captured PNG paths
      - clue_log: list of reasoning/hint strings emitted by the analyst
      - best_guess: current best location guess (region/country level, never full answer)
      - confidence: 0-100 integer
      - answer_revealed: whether the user has triggered the reveal
      - status: "active" | "closed"
    """

    MODE_NAME = "game_mode"
    SESSIONS_KEY = "geoguessr_sessions"
    INDEX_BY_ROOM_KEY = "geoguessr_sessions_by_room"

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
            raise ValueError("room_id is required for geoguessr binding key")
        if not surf:
            raise ValueError("surface is required for geoguessr binding key")
        return f"{surf}::{rid}::{ctx}"

    def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        raw = self._blackboard.get_state_value(self.SESSIONS_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid geoguessr sessions state type: %s", type(raw))
            raise RuntimeError("Invalid geoguessr_sessions state")
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
            logger.error("Invalid geoguessr room index type: %s", type(raw))
            raise RuntimeError("Invalid geoguessr_sessions_by_room state")
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
        session_id = f"geo_{uuid.uuid4().hex[:12]}"
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
            "screenshot_paths": [],
            "clue_log": [],
            "best_guess": "",
            "confidence": 0,
            "answer_revealed": False,
            "tts_requested": False,
        }
        sessions[session_id] = payload
        room_index[room_key] = session_id
        self._save_sessions(sessions)
        self._save_room_index(room_index)
        logger.debug("[GeoguessrSessionService] activated session=%s room_key=%s", session_id, room_key)
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

    def get_session(self, *, session_id: str) -> dict[str, Any] | None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        return dict(payload) if isinstance(payload, dict) else None

    def add_screenshot(self, *, session_id: str, screenshot_path: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Geoguessr session not found: {session_id}")
        paths: List[str] = list(payload.get("screenshot_paths") or [])
        paths.append(str(screenshot_path))
        payload["screenshot_paths"] = paths
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def update_analysis(
        self,
        *,
        session_id: str,
        best_guess: str,
        confidence: int,
        clue: str,
    ) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Geoguessr session not found: {session_id}")
        payload["best_guess"] = str(best_guess or "")
        payload["confidence"] = max(0, min(100, int(confidence or 0)))
        clues: List[str] = list(payload.get("clue_log") or [])
        if clue:
            clues.append(str(clue))
        payload["clue_log"] = clues
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def mark_revealed(self, *, session_id: str) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Geoguessr session not found: {session_id}")
        payload["answer_revealed"] = True
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def set_tts_requested(self, *, session_id: str, tts_requested: bool) -> None:
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Geoguessr session not found: {session_id}")
        payload["tts_requested"] = bool(tts_requested)
        sessions[session_id] = payload
        self._save_sessions(sessions)

    def reset_round(self, *, session_id: str) -> None:
        """Clear game state for a new round while keeping the session active."""
        sessions = self._load_sessions()
        payload = sessions.get(session_id)
        if not isinstance(payload, dict):
            raise ValueError(f"Geoguessr session not found: {session_id}")
        payload["screenshot_paths"] = []
        payload["clue_log"] = []
        payload["best_guess"] = ""
        payload["confidence"] = 0
        payload["answer_revealed"] = False
        payload["updated_at_utc"] = self._utc_now_iso()
        sessions[session_id] = payload
        self._save_sessions(sessions)
        logger.debug("[GeoguessrSessionService] round reset session=%s", session_id)

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
        logger.debug("[GeoguessrSessionService] deactivated session=%s reason=%s", session_id, reason)
        return True
