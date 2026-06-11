import os
from typing import Any

from app.assistant.utils.time_utils import day_reset_cutoff_utc, to_utc, utc_to_local


class HistoryFormatter:
    """
    Build condensed recent-history text for prompt injection.
    """

    def format_recent_history(self, agent_messages: list[Any]) -> str:
        allowed = {
            "tool_result",
            "agent_result",
            "tool_request",
            "tool_result_summary",
            "agent_request",
        }

        def _safe_content(m: Any) -> str:
            c = getattr(m, "content", "")
            return "" if c is None else str(c).strip()

        def _attachment_markers(m: Any) -> str:
            meta = getattr(m, "metadata", None)
            if not isinstance(meta, dict):
                return ""
            atts = meta.get("attachments")
            if not isinstance(atts, list) or not atts:
                return ""
            lines: list[str] = []
            for att in atts:
                if not isinstance(att, dict):
                    continue
                if att.get("type") != "image":
                    continue
                p = att.get("path")
                if not isinstance(p, str) or not p.strip():
                    continue
                fname = att.get("original_filename") or os.path.basename(p)
                lines.append(f"[image attached: {fname}]")
            return "\n".join(lines).strip()

        def _tool_result_ref_markers(m: Any) -> str:
            meta = getattr(m, "metadata", None)
            if not isinstance(meta, dict):
                return ""
            tool_result_id = meta.get("tool_result_id")
            if isinstance(tool_result_id, str) and tool_result_id.strip():
                return f"[tool_result_id: {tool_result_id.strip()}]"
            return ""

        def _prefix(m: Any) -> str:
            if bool(getattr(m, "is_chat", False)):
                sender = (
                    getattr(m, "room_speaker_name", None)
                    or getattr(m, "sender", None)
                    or getattr(m, "role", None)
                    or "Unknown"
                )
                return str(sender)

            data_type = str(getattr(m, "data_type", "") or "").strip().lower()
            sub = getattr(m, "sub_data_type", None)
            tool_name = str(sub[0]).strip() if isinstance(sub, list) and sub and isinstance(sub[0], str) else ""

            if data_type == "tool_request":
                return f"Tool call ({tool_name})" if tool_name else "Tool call"
            if data_type == "tool_result":
                return f"Tool result ({tool_name})" if tool_name else "Tool result"
            if data_type == "tool_result_summary":
                return f"Tool summary ({tool_name})" if tool_name else "Tool summary"
            if data_type == "agent_request":
                return "Agent call"
            if data_type == "agent_result":
                sender = getattr(m, "sender", None)
                if isinstance(sender, str) and sender.strip():
                    return f"Agent message ({sender.strip()})"
                return "Agent message"
            return data_type or "Message"

        indexed = list(enumerate(agent_messages or []))
        filtered: list[tuple[int, Any]] = [
            (idx, msg)
            for idx, msg in indexed
            if bool(getattr(msg, "is_chat", False)) or getattr(msg, "data_type", None) in allowed
        ]

        sorted_msgs = sorted(
            filtered,
            key=lambda it: (to_utc(getattr(it[1], "timestamp", None)) is None, to_utc(getattr(it[1], "timestamp", None)), it[0]),
        )

        cutoff_utc = day_reset_cutoff_utc()
        old_chat_header_added = False
        today_chat_header_added = False
        pieces: list[str] = []
        for _, m in sorted_msgs:
            content = _safe_content(m)
            extra_a = _attachment_markers(m)
            extra_r = _tool_result_ref_markers(m)
            body = "\n".join([x for x in (content, extra_a, extra_r) if x]).strip()
            if not body:
                continue

            ts_utc = to_utc(getattr(m, "timestamp", None))
            if ts_utc is not None:
                if bool(getattr(m, "is_chat", False)):
                    if ts_utc < cutoff_utc and not old_chat_header_added:
                        pieces.append("CHAT FROM YESTERDAY")
                        old_chat_header_added = True
                    if ts_utc >= cutoff_utc and not today_chat_header_added:
                        pieces.append("CHAT FROM TODAY")
                        today_chat_header_added = True
                local_ts = utc_to_local(ts_utc)
                # For messages older than the current local-day window, include explicit date.
                if ts_utc < cutoff_utc and bool(getattr(m, "is_chat", False)):
                    time_label = local_ts.strftime("%Y-%m-%d %H:%M")
                else:
                    time_label = local_ts.strftime("%H:%M")
                header = f"[{time_label}] {_prefix(m)}:"
            else:
                header = f"{_prefix(m)}:"
            pieces.append(f"{header} {body}".strip())

        return "\n\n".join(pieces).strip()

    def format_latest_exchange(self, agent_messages: list[Any]) -> str:
        """
        Return only the last user message + the assistant reply that immediately follows it,
        formatted the same way as format_recent_history.

        Used by doc_writer to identify exactly what changed this turn.
        """
        indexed = list(enumerate(agent_messages or []))
        chat_msgs = [
            (idx, msg)
            for idx, msg in indexed
            if bool(getattr(msg, "is_chat", False))
        ]
        if not chat_msgs:
            return ""

        # Find the last user message
        last_user_idx = None
        for i in range(len(chat_msgs) - 1, -1, -1):
            m = chat_msgs[i][1]
            role = str(getattr(m, "role", "") or "").strip().lower()
            sender = str(getattr(m, "sender", "") or "").strip().lower()
            if role == "user" or sender == "user":
                last_user_idx = i
                break

        if last_user_idx is None:
            # No user message found — return the last message only
            exchange_msgs = [chat_msgs[-1]]
        else:
            # Include last user message + any assistant messages after it
            exchange_msgs = chat_msgs[last_user_idx:]

        pieces: list[str] = []
        for _, m in exchange_msgs:
            content = str(getattr(m, "content", "") or "").strip()
            if not content:
                continue
            from datetime import datetime, timezone
            ts = getattr(m, "timestamp", None)
            if ts is not None:
                try:
                    if isinstance(ts, str):
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if getattr(ts, "tzinfo", None) is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    from app.assistant.utils.time_utils import utc_to_local
                    time_label = utc_to_local(ts).strftime("%H:%M")
                    header = f"[{time_label}]"
                except Exception:
                    header = ""
            else:
                header = ""

            sender = (
                getattr(m, "room_speaker_name", None)
                or getattr(m, "sender", None)
                or getattr(m, "role", None)
                or "Unknown"
            )
            prefix = f"{header} {sender}:".strip()
            pieces.append(f"{prefix} {content}".strip())

        return "\n\n".join(pieces).strip()

