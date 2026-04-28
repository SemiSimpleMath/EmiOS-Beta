"""
Base class for editor-style rooms (task spec, doc, etc.).

Editor rooms manage a persistent artifact (markdown document) that is
collaboratively edited through conversation. Each room type subclasses
this and defines how to load/save its artifact.

Lifecycle per turn:
  1. ensure_session() — find or create a session for this room
  2. restore_state(blackboard) — load artifact from session onto blackboard
  3. (manager runs normally)
  4. persist_state(blackboard) — save updated artifact back to session
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class EditorRoom:
    """Base class for rooms that edit a persistent artifact."""

    def __init__(self, room_id: str):
        self.room_id = room_id

    def ensure_session(self, *, blackboard, surface: str = "ui", context_id: str = "main", room_id: str = "") -> str:
        """Find or create a session. Returns session_id."""
        raise NotImplementedError

    def restore_state(self, *, blackboard, session_id: str) -> None:
        """Load persisted artifact and put it on the blackboard."""
        raise NotImplementedError

    def persist_state(self, *, blackboard, session_id: str) -> None:
        """Save current artifact from blackboard back to session."""
        raise NotImplementedError

    def prepare_turn(self, *, blackboard, surface: str = "ui", context_id: str = "main", room_id: str = "") -> None:
        """Full pre-turn setup: ensure session + restore state."""
        session_id = self.ensure_session(
            blackboard=blackboard, surface=surface, context_id=context_id,
            room_id=room_id or self.room_id,
        )
        if session_id:
            self.restore_state(blackboard=blackboard, session_id=session_id)

    def finalize_turn(self, *, blackboard) -> None:
        """Post-turn: persist state back to session."""
        session_id = str(blackboard.get_state_value("task_creation_session_id", "") or "").strip()
        if session_id:
            self.persist_state(blackboard=blackboard, session_id=session_id)
