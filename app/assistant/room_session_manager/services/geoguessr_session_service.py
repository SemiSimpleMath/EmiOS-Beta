from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.room_session_manager.services.room_binding_session_service import (
    RoomBindingSessionService,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class GeoguessrSessionService(RoomBindingSessionService):
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
    SESSION_ID_PREFIX = "geo"
    LABEL = "geoguessr"

    def _initial_payload_extras(self) -> Dict[str, Any]:
        return {
            "screenshot_paths": [],
            "clue_log": [],
            "best_guess": "",
            "confidence": 0,
            "answer_revealed": False,
            "tts_requested": False,
        }

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
