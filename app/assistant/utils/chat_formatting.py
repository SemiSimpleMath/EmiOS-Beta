from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import to_utc, utc_to_local


def messages_to_chat_excerpts(
    messages: List[Message],
    *,
    consumer_room_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Convert Message objects into stable, LLM-friendly excerpt dicts.

    Output keys intentionally match existing downstream expectations:
    - timestamp_utc: ISO string
    - time_local: human-readable local time
    - sender: sender string
    - content: message content
    - origin_room_id: room where the message originated
    - cross_room_tf: True when message origin differs from consumer_room_id
    - actionability: "local_actionable" or "external_context_only"

    Sender and content are the message's own. Until 2026-11-15 a cross-room message had "[room:<id>]" appended to
    its sender and "[CROSS_ROOM_CONTEXT_ONLY]" prefixed to its content, and pipeline prompts (daily_context_tracker,
    dayflow_cron_tickets), which read the owner's chat rooms on purpose, showed the raw markers on every line. A room
    seeding another room's history tags it itself (room_session_manager).
    """

    consumer_room = str(consumer_room_id or "").strip()
    out: List[Dict[str, Any]] = []
    for m in messages or []:
        try:
            meta = getattr(m, "metadata", None)
            if isinstance(meta, dict) and meta.get("context_suppressed") and not meta.get("context_pinned"):
                continue
            ts_utc = to_utc(getattr(m, "timestamp", None))
            if ts_utc is None:
                continue
            origin_room_id = str(getattr(m, "room_id", "") or "").strip()
            cross_room_tf = bool(consumer_room and origin_room_id and origin_room_id != consumer_room)
            sender = getattr(m, "sender", "") or ""
            content = getattr(m, "content", "") or ""
            out.append(
                {
                    "timestamp_utc": ts_utc.isoformat(),
                    "time_local": utc_to_local(ts_utc).strftime("%I:%M %p"),
                    "sender": sender,
                    "content": content,
                    "origin_room_id": origin_room_id,
                    "cross_room_tf": cross_room_tf,
                    "actionability": "external_context_only" if cross_room_tf else "local_actionable",
                }
            )
        except Exception:
            continue
    return out


def messages_to_chat_history_text(
    messages: List[Message],
    *,
    consumer_room_id: Optional[str] = None,
) -> str:
    """
    Convert Message objects into a compact multi-line chat history string.
    """

    excerpts = messages_to_chat_excerpts(
        messages,
        consumer_room_id=consumer_room_id,
    )
    lines = [f"[{m['time_local']}] {m['sender']}: {m['content']}" for m in excerpts]
    return "\n".join(lines)

