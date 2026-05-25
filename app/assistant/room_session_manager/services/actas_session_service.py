from __future__ import annotations

from typing import Any, Dict

from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ActAsSessionService:
    """Per-room sticky principal binding for the `/actas` slash command.

    Stores which principal the user is currently acting as (e.g. ``"emi"``)
    for a given (surface, room_id, context_id) tuple. The binding survives
    across messages until explicitly cleared via ``/actas user`` or ``/end``.

    Unlike the other session services in this package, the only state per
    room is the principal name itself — no drafts, no compiled IDs, no
    session_id. The principal is read on every inbound envelope by the
    room scope builder and stamped onto ``ScopeContext.acting_as``.

    Default behavior when no binding exists: principal = ``"user"``.
    """

    STATE_KEY = "actas_principal_by_room"

    def __init__(self, *, blackboard: GlobalBlackBoard) -> None:
        self._blackboard = blackboard

    @staticmethod
    def _room_key(*, room_id: str, surface: str, context_id: str) -> str:
        rid = str(room_id or "").strip()
        surf = str(surface or "").strip().lower()
        ctx = str(context_id or "main").strip() or "main"
        if not rid:
            raise ValueError("room_id is required for actas binding key")
        if not surf:
            raise ValueError("surface is required for actas binding key")
        return f"{surf}::{rid}::{ctx}"

    def _load(self) -> Dict[str, str]:
        raw = self._blackboard.get_state_value(self.STATE_KEY, {})
        if not isinstance(raw, dict):
            logger.error("Invalid actas principal state type: %s", type(raw))
            raise RuntimeError("Invalid actas_principal_by_room state")
        out: Dict[str, str] = {}
        for k, v in raw.items():
            if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip():
                out[k] = v.strip().lower()
        return out

    def _save(self, mapping: Dict[str, str]) -> None:
        if not isinstance(mapping, dict):
            raise TypeError("actas mapping must be a dict")
        self._blackboard.update_state_value(self.STATE_KEY, mapping)

    def get_principal(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
    ) -> str:
        """Return the active principal for this room, or ``"user"`` if none."""
        key = self._room_key(room_id=room_id, surface=surface, context_id=context_id)
        mapping = self._load()
        return mapping.get(key, "user")

    def set_principal(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
        principal: str,
    ) -> None:
        """Set the room's active principal. ``principal="user"`` clears the binding."""
        normalized = str(principal or "user").strip().lower() or "user"
        key = self._room_key(room_id=room_id, surface=surface, context_id=context_id)
        mapping = dict(self._load())
        if normalized == "user":
            mapping.pop(key, None)
        else:
            mapping[key] = normalized
        self._save(mapping)
        logger.debug("actas binding for %s -> %s", key, normalized)

    def clear_principal(
        self,
        *,
        room_id: str,
        surface: str,
        context_id: str,
    ) -> bool:
        """Drop the room's active principal. Returns True iff one was active."""
        key = self._room_key(room_id=room_id, surface=surface, context_id=context_id)
        mapping = dict(self._load())
        if key in mapping:
            mapping.pop(key)
            self._save(mapping)
            logger.debug("actas binding cleared for %s", key)
            return True
        return False
