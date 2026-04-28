"""
Room mode exit service.

Writes a brief summary note into room chat history when a specialised
mode (planning / task creation / doc creation / game) finishes, so that
subsequent chat_gate turns have context about what was accomplished.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_MODE_FLAGS = {
    "planning_mode_done_tf":      "planning",
    "task_creation_mode_done_tf": "task_creation",
    "doc_creation_mode_done_tf":  "doc_creation",
}

_MODE_LABELS = {
    "planning":      "Planning session",
    "task_creation": "Task creation session",
    "doc_creation":  "Document creation session",
    "game":          "GeoGuessr game",
}


def maybe_write_mode_exit_summary(
        *,
        blackboard: Any,
        envelope: InboundEnvelope,
        payload: Dict[str, Any],
        reply_text: str,
) -> None:
    if not isinstance(payload, dict):
        return

    game_over = bool(payload.get("game_over", False))

    active_mode: str | None = None
    for flag, mode in _MODE_FLAGS.items():
        if bool(payload.get(flag, False)):
            active_mode = mode
            break
    if active_mode is None and game_over:
        active_mode = "game"
    if active_mode is None:
        return

    label = _MODE_LABELS.get(active_mode, active_mode)

    plan_summary = str(payload.get("planning_mode_summary") or "").strip()
    agent_reply = str(reply_text or "").strip()
    if plan_summary:
        summary = f"[{label} completed] {plan_summary}"
    elif agent_reply and len(agent_reply) <= 200:
        summary = f"[{label} completed] {agent_reply}"
    else:
        extra = ""
        if active_mode == "task_creation":
            spec = str(payload.get("task_spec_markdown") or "").strip()
            if spec:
                title_line = next((l.lstrip("#").strip() for l in spec.splitlines() if l.strip()), "")
                if title_line:
                    extra = f" Task: \"{title_line}\"."
        elif active_mode == "doc_creation":
            doc_name = str(payload.get("doc_name") or "").strip()
            if doc_name:
                extra = f" Document: \"{doc_name}\"."
        summary = f"[{label} completed]{extra}"

    try:
        note = Message(
            data_type="mode_exit_summary",
            sender="system",
            role="assistant",
            content=summary,
            is_chat=True,
            request_id=envelope.request_id,
            timestamp=datetime.now(timezone.utc),
            room_id=envelope.room_id,
            room_surface=envelope.surface,
            room_context_id=envelope.context_id,
            room_visibility="owner_only",
            room_message_direction="outbound",
            room_initiated_by="system",
            room_delivery_mode="auto_send",
            room_speaker_id="system",
            room_speaker_name="system",
            room_speaker_role="assistant",
            metadata={
                "room_mode": "normal",
                "mode_exit_summary": True,
                "exited_mode": active_mode,
            },
        )
        blackboard.add_msg(note)
        logger.info(
            "[room_session_manager] wrote mode exit summary for room=%s mode=%s",
            envelope.room_id,
            active_mode,
        )
    except Exception as e:
        logger.error("Failed writing mode exit summary for room=%s mode=%s: %s", envelope.room_id, active_mode, e)
        logger.debug("mode exit summary write exception details", exc_info=True)
        raise
