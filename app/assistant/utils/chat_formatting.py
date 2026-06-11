from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import to_utc, utc_to_local


def messages_to_chat_excerpts(
    messages: List[Message],
    *,
    consumer_room_id: Optional[str] = None,
    cross_room_prefix: str = "[CROSS_ROOM_CONTEXT_ONLY]",
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
            if cross_room_tf:
                # Hard tag: cross-room content is context only.
                sender = f"{sender} [room:{origin_room_id}]"
                content = f"{cross_room_prefix} {content}".strip()
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
    cross_room_prefix: str = "[CROSS_ROOM_CONTEXT_ONLY]",
) -> str:
    """
    Convert Message objects into a compact multi-line chat history string.
    """

    excerpts = messages_to_chat_excerpts(
        messages,
        consumer_room_id=consumer_room_id,
        cross_room_prefix=cross_room_prefix,
    )
    lines = [f"[{m['time_local']}] {m['sender']}: {m['content']}" for m in excerpts]
    return "\n".join(lines)

