from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable

from app.assistant.utils.time_utils import utc_to_local, parse_time_string


def _safe_content(m: Any) -> str:
    c = getattr(m, "content", "")
    return "" if c is None else str(c).strip()


def _meta(m: Any) -> dict:
    meta = getattr(m, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _history_deleted(m: Any) -> bool:
    return bool(_meta(m).get("history_deleted", False))


def _history_hidden(m: Any) -> bool:
    return bool(_meta(m).get("history_hidden", False))


def _history_summary(m: Any) -> str:
    return str(_meta(m).get("history_summary", "") or "").strip()


def _is_internal_agent_result(m: Any) -> bool:
    dt = str(getattr(m, "data_type", "") or "").strip().lower()
    if dt != "agent_result":
        return False
    sender = str(getattr(m, "sender", "") or "").strip().lower()
    return sender in {"shared::tool_arguments"}


def _display_tool_call_payload(tool_name: str, args: str | None) -> str:
    parsed_args: Any = {}
    if isinstance(args, str) and args.strip():
        raw = args.strip()
        try:
            parsed_args = json.loads(raw)
        except Exception:
            parsed_args = raw
    payload = {"tool_name": tool_name, "arguments": parsed_args}
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def _tool_result_ref_markers(m: Any) -> str:
    meta = _meta(m)
    tool_result_id = meta.get("tool_result_id")
    parts: list[str] = []
    if isinstance(tool_result_id, str) and tool_result_id.strip():
        parts.append(f"Result ID: {tool_result_id.strip()}")
    return "\n".join(parts).strip()

def _tool_result_summary_link_markers(m: Any) -> str:
    meta = _meta(m)
    tid = meta.get("summarizes_tool_result_id")
    parts: list[str] = []
    if isinstance(tid, str) and tid.strip():
        parts.append(f"Result ID: {tid.strip()}")
    if parts:
        return "\n".join(parts).strip()
    return ""


def _fmt_time(m: Any) -> str:
    ts = getattr(m, "timestamp", None)
    if not ts:
        return ""
    try:
        local_ts = utc_to_local(ts)
        return local_ts.strftime("%H:%M")
    except Exception:
        return ""


def _fmt_header(idx: int, time_s: str, title: str) -> str:
    parts = [f"[{idx}]"]
    if time_s:
        parts.append(time_s)
    if title:
        parts.append(f"- {title}")
    return " ".join(parts).strip()


def format_recent_history(agent_messages: Iterable[Any]) -> str:
    """
    Produce a readable, chronological, numbered recent_history string.

    Intended behavior for planner readability:
    - Summaries are first-class history messages (tool_result_summary).
    - Once a tool_result has been summarized (summary links to tool_result_id),
      suppress that older tool_result in history to keep context compact.
    - Always keep the most recent tool_result(s) that have not yet been summarized.
    """
    ALLOWED = {
        "tool_result",
        "agent_result",
        "tool_request",
        "tool_result_summary",
        "agent_request",
    }
    def _is_suppressed(m: Any) -> bool:
        meta = _meta(m)
        pinned = bool(meta.get("context_pinned", False))
        if pinned:
            return False
        return bool(meta.get("context_suppressed", False))

    msgs = [
        m
        for m in list(agent_messages or [])
        if getattr(m, "data_type", None) in ALLOWED and not _is_suppressed(m) and not _is_internal_agent_result(m)
    ]

    def _require_time(m: Any) -> datetime:
        ts = getattr(m, "timestamp", None)
        if not ts:
            raise RuntimeError("[history_formatting] Missing timestamp on message; cannot sort history.")
        return parse_time_string(ts)

    # Sort strictly by timestamp (ascending). If timestamps collide, preserve insertion order.
    msgs = sorted(
        enumerate(msgs),
        key=lambda it: (_require_time(it[1]), it[0]),
    )
    msgs = [m for _idx, m in msgs]

    def _emit(idx: int, time_s: str, title: str, body_lines: list[str]) -> str:
        header = _fmt_header(idx, time_s, title)
        extras: list[str] = []
        extra = "\n".join([x for x in extras if x]).strip()
        body = "\n".join([x for x in body_lines if x]).strip()
        combined = "\n".join([x for x in (header, body, extra) if x]).strip()
        return combined

    pieces: list[str] = []
    i = 0
    n = len(msgs)
    pending_request: dict[str, Any] | None = None
    pending_agent_request: dict[str, Any] | None = None
    by_tool_result_id: dict[str, dict[str, Any]] = {}
    last_tool_result_id: str | None = None

    summarized_ids: set[str] = set()
    for m in msgs:
        if getattr(m, "data_type", None) != "tool_result_summary":
            continue
        meta = getattr(m, "metadata", None)
        if not isinstance(meta, dict):
            continue
        # Only actual summaries (from the summary agent) should hide tool results.
        # Planner notes (annotation_type="note") are a notepad — they must NOT
        # replace the full tool result.
        if str(meta.get("annotation_type") or "").strip().lower() == "note":
            continue
        tid = meta.get("summarizes_tool_result_id")
        tid = tid.strip() if isinstance(tid, str) else None
        if tid:
            summarized_ids.add(tid)

    def _parse_tool_request(content: str) -> tuple[str | None, str | None]:
        m = re.match(r"Calling tool\\s+([^\\s]+)\\s+with arguments\\s+(.+)", content.strip())
        if not m:
            return None, content.strip() or None
        return m.group(1), m.group(2)

    def _parse_agent_request(content: str) -> tuple[str | None, str | None]:
        m = re.match(r"Calling agent\\s+'([^']+)'\\s+with arguments:\\s+(.+)", content.strip())
        if not m:
            return None, None
        return m.group(1), m.group(2)

    def _finalize_pending_request() -> None:
        nonlocal pending_request
        if not pending_request:
            return
        time_s = pending_request.get("time_s", "")
        tool_name = pending_request.get("tool_name") or "tool"
        hid = pending_request.get("history_id", 0)
        args = pending_request.get("args")
        lines = []
        if args:
            lines.append(f"Args: {args}")
        combined = _emit(hid, time_s, tool_name, lines)
        if combined:
            pieces.append(combined)
        pending_request = None

    while i < n:
        m = msgs[i]
        dt = getattr(m, "data_type", None)

        if dt == "tool_request":
            req_hid = _meta(m).get("history_id", 0)
            content = _safe_content(m)
            agent_name, agent_args = _parse_agent_request(content)
            if agent_name:
                pending_agent_request = {
                    "time_s": _fmt_time(m),
                    "agent_name": agent_name,
                    "args": agent_args,
                    "history_id": req_hid,
                }
            else:
                tool_name, args = _parse_tool_request(content)
                pending_request = {
                    "time_s": _fmt_time(m),
                    "tool_name": tool_name or "tool",
                    "args": args,
                    "history_id": req_hid,
                }
            i += 1
            continue

        if dt == "tool_result":
            if _history_deleted(m):
                i += 1
                continue
            meta = _meta(m)
            hid = meta.get("history_id", 0)
            tid = meta.get("tool_result_id")
            tid = tid.strip() if isinstance(tid, str) else None
            if tid:
                last_tool_result_id = tid
            sub = getattr(m, "sub_data_type", None)
            tool_name = None
            if isinstance(sub, list) and sub:
                tool_name = str(sub[0]) if sub[0] else None
            summary_text = _history_summary(m)
            if _history_hidden(m) and not summary_text:
                i += 1
                continue
            content_str = summary_text if summary_text else _safe_content(m)
            entry = {
                "history_id": hid,
                "time_s": _fmt_time(m),
                "tool_name": tool_name or (pending_request or {}).get("tool_name") or "tool",
                "args": (pending_request or {}).get("args"),
                "tool_result_id": tid,
                "summary": None,
                "content": content_str,
            }
            if pending_request and pending_request.get("time_s"):
                entry["time_s"] = pending_request.get("time_s")
            if tid and tid in summarized_ids:
                by_tool_result_id[tid] = entry
            else:
                lines: list[str] = []
                lines.append(
                    f"TOOL CALL: {_display_tool_call_payload(str(entry.get('tool_name') or 'tool'), entry.get('args'))}"
                )
                if entry.get("tool_result_id"):
                    lines.append(f"Result ID: {entry['tool_result_id']}")
                if entry.get("content"):
                    lines.append(f"Summary: {entry['content']}" if summary_text else f"Result: {entry['content']}")
                combined = _emit(hid, entry.get("time_s", ""), entry.get("tool_name") or "tool", lines)
                if combined:
                    pieces.append(combined)
            if pending_request:
                pending_request = None
            i += 1
            continue

        if dt == "tool_result_summary":
            meta = getattr(m, "metadata", None) if not isinstance(getattr(m, "metadata", None), dict) else m.metadata
            if not isinstance(meta, dict):
                meta = {}
            tid = meta.get("summarizes_tool_result_id")
            tid = tid.strip() if isinstance(tid, str) else None
            summary = _safe_content(m)
            annotation_type = str(meta.get("annotation_type") or "").strip().lower()
            summary_label = "Note" if annotation_type == "note" else "Summary"
            entry = by_tool_result_id.get(tid) if tid else None
            if entry is None:
                entry = {
                    "history_id": meta.get("history_id", 0),
                    "time_s": _fmt_time(m),
                    "tool_name": "tool",
                    "args": None,
                    "tool_result_id": tid,
                    "summary": summary,
                }
            else:
                entry["summary"] = summary
                if not entry.get("time_s"):
                    entry["time_s"] = _fmt_time(m)

            hid = entry.get("history_id", 0)
            lines: list[str] = []
            lines.append(
                f"TOOL CALL: {_display_tool_call_payload(str(entry.get('tool_name') or 'tool'), entry.get('args'))}"
            )
            if entry.get("tool_result_id"):
                lines.append(f"Result ID: {entry['tool_result_id']}")
            if entry.get("summary"):
                lines.append(f"{summary_label}: {entry['summary']}")
            combined = _emit(hid, entry.get("time_s", ""), entry.get("tool_name") or "tool", lines)
            if combined:
                pieces.append(combined)
            if tid and tid in by_tool_result_id:
                del by_tool_result_id[tid]
            i += 1
            continue

        if dt in ("agent_result", "agent_request"):
            hid = _meta(m).get("history_id", 0)
            if dt == "agent_result" and pending_agent_request:
                time_s = pending_agent_request.get("time_s", "") or _fmt_time(m)
                agent_name = pending_agent_request.get("agent_name") or getattr(m, "sender", None) or "agent"
                args = pending_agent_request.get("args")
                lines: list[str] = []
                if args:
                    lines.append(f"Args: {args}")
                result_body = _safe_content(m)
                if result_body:
                    lines.append(f"Result: {result_body}")
                combined = _emit(hid, time_s, f"Calling agent {agent_name}", lines)
                if combined:
                    pieces.append(combined)
                pending_agent_request = None
                i += 1
                continue
            time_s = _fmt_time(m)
            title = getattr(m, "sender", None) or "agent"
            body = _safe_content(m)
            combined = _emit(hid, time_s, title, [body] if body else [])
            if combined:
                pieces.append(combined)
            i += 1
            continue

        i += 1

    if by_tool_result_id:
        missing = ", ".join(sorted(by_tool_result_id.keys()))
        raise RuntimeError(
            "[history_formatting] Missing tool_result_summary for tool_result_id(s): "
            + missing
        )

    _finalize_pending_request()

    return "\n\n".join(pieces).strip()

