"""
Task spec editor room.

Manages a task spec document through conversational editing.
Persistence via unified_log (task_spec_store), not session service.
"""
from __future__ import annotations

from app.assistant.room_handlers.editor_room import EditorRoom
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

BLANK_SPEC = (
    "## Title\n[New Task]\n\n"
    "## Description\n[TBD]\n\n"
    "## Steps\n[No steps defined yet]\n\n"
    "## Completion\n[TBD]"
)


class TaskSpecRoom(EditorRoom):
    """Room for creating and editing task specs."""

    def __init__(self, room_id: str = "task_create"):
        super().__init__(room_id)

    def ensure_session(self, *, blackboard, surface: str = "ui", context_id: str = "main", room_id: str = "") -> str:
        """Ensure a task_id exists for this editing session. Returns session_id (= task_id)."""
        # The task_id IS the session identity.
        existing = str(blackboard.get_state_value("task_creation_session_id", "") or "").strip()
        if existing:
            return existing

        # Extract task_id from the room_id (e.g., task_spec::spammer_finder → spammer_finder).
        effective_room_id = room_id or self.room_id
        if "::" in effective_room_id:
            task_id = effective_room_id.split("::", 1)[1]
            blackboard.update_state_value("task_creation_session_id", task_id)
            return task_id

        # Fallback: create a new task.
        from app.assistant.lib.task_utils.task_spec_store import make_task_id
        task_id = make_task_id()
        blackboard.update_state_value("task_creation_session_id", task_id)
        return task_id

    def restore_state(self, *, blackboard, session_id: str) -> None:
        """Load the spec from unified_log onto the blackboard.

        Invariants:
        - On success, both ``spec`` and ``current_spec`` are set to the loaded
          content so downstream consumers reading either key see the same value.
        - On miss (no draft in unified_log), only seed BLANK_SPEC if the
          blackboard has nothing yet. Never clobber an existing non-empty spec
          on the blackboard — that would let a stale persist_state overwrite
          real content with a blank.
        """
        from app.assistant.lib.task_utils.task_spec_store import load_task_spec_draft

        draft = load_task_spec_draft(session_id)
        if draft and draft.get("spec_markdown"):
            spec_md = draft["spec_markdown"]
            blackboard.update_state_value("spec", spec_md)
            blackboard.update_state_value("current_spec", spec_md)
            wm = draft.get("editor_watermark", 0)
            if wm:
                blackboard.update_state_value("task_spec_editor_watermark_msg_id", wm)
            logger.debug(
                "[TaskSpecRoom] Restored spec (%d chars) from unified_log for task %s",
                len(spec_md), session_id,
            )
            return

        existing = str(blackboard.get_state_value("spec", "") or "")
        if not existing.strip():
            blackboard.update_state_value("spec", BLANK_SPEC)
            blackboard.update_state_value("current_spec", BLANK_SPEC)

    def persist_state(self, *, blackboard, session_id: str) -> None:
        """Save the current spec from the blackboard back to unified_log.

        The spec is written verbatim — no stripping. Users may author specs
        with trailing whitespace or newlines and a round-trip should be lossless.
        """
        from app.assistant.lib.task_utils.task_spec_store import save_task_spec_draft

        draft_raw = blackboard.get_state_value("spec", "")
        draft = "" if draft_raw is None else str(draft_raw)
        if not draft.strip():
            return
        watermark = blackboard.get_state_value("task_spec_editor_watermark_msg_id")
        wm_int = int(watermark) if watermark is not None else None
        save_task_spec_draft(
            session_id,
            spec_markdown=draft,
            watermark=wm_int,
            caller="TaskSpecRoom.persist_state",
        )

    def finalize_turn(self, *, blackboard) -> None:
        """Post-turn: persist state back to unified_log."""
        session_id = str(blackboard.get_state_value("task_creation_session_id", "") or "").strip()
        if session_id:
            self.persist_state(blackboard=blackboard, session_id=session_id)
