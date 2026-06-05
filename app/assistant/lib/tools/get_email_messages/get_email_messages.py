from __future__ import annotations

import re
from email import message_from_string
from email.utils import parsedate_to_datetime
from datetime import timezone
from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.email_tool.utils.email_processor import EmailProcessor
from app.assistant.lib.core_tools.email_tool.utils.email_utils import EmailUtils
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class GetEmailMessagesTool(BaseTool):
    """
    Search Gmail by query; return lightweight per-message HEADERS only.

    Two-phase retrieval (search -> hydrate): this tool returns
    id/subject/sender/date + a short snippet + any meeting link — never full
    bodies. To read a message's full body, hydrate the specific message with
    ``get_email_thread(message_id)``. This keeps a wide match (dozens of rows)
    small enough to never overflow the manager's working context — full bodies
    inline were a context bomb (2026-06 Friday-Night-Meats incident: 200 bodies
    -> 94K-token context -> manager cycle-budget exhaustion).
    """

    # A URL that points at a known video-meeting host. We surface this one
    # body-derived signal (the join link) into the headers result so the agent
    # can see "there's a Zoom here" without hydrating the whole body.
    _URL_RE = re.compile(r"https?://[^\s<>\"']+")
    _MEETING_HOSTS = ("zoom.us/j/", "meet.google.com/", "teams.microsoft.com/", "teams.live.com/")

    def __init__(self):
        super().__init__("get_email_messages")
        self._email_utils: EmailUtils | None = None

    def _utils(self) -> EmailUtils:
        if self._email_utils is None:
            self._email_utils = EmailUtils()
        return self._email_utils

    @staticmethod
    def _parse_query(raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            raise ValueError("query is required and must be non-empty.")
        return value

    @staticmethod
    def _parse_max_results(raw: Any) -> int:
        if raw is None:
            return 25
        try:
            value = int(raw)
        except Exception as e:
            logger.error("Invalid max_results value: %r", raw)
            logger.debug("get_email_messages max_results parse exception details", exc_info=True)
            raise ValueError(f"max_results must be an integer, got {raw!r}") from e
        if value < 1:
            raise ValueError("max_results must be >= 1")
        # Hard cap: a large pull of full email bodies is a context bomb — one
        # result can dwarf the manager's whole working context and defeat the
        # summary compactor (which keeps the latest N results raw). Clamp to 50.
        return min(value, 50)

    @staticmethod
    def _snippet(body: str, limit: int = 240) -> str:
        """Single-line preview: collapse whitespace/newlines, cap length."""
        text = " ".join(str(body or "").split())
        return text[:limit]

    @classmethod
    def _extract_meeting_link(cls, body: str) -> str:
        """First URL in the body that points at a known video-meeting host, or ''."""
        if not body:
            return ""
        for url in cls._URL_RE.findall(body):
            u = url.rstrip(").,;>\"'")
            if any(host in u.lower() for host in cls._MEETING_HOSTS):
                return u
        return ""

    @staticmethod
    def _to_utc_iso(raw_date: str) -> str:
        value = str(raw_date or "").strip()
        if not value:
            return ""
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                return ""
            return dt.astimezone(timezone.utc).isoformat()
        except Exception as e:
            logger.error("Failed to parse message date '%s' to ISO.", value)
            logger.debug("get_email_messages date parse exception details", exc_info=True)
            raise ValueError(f"Invalid message date format: {value!r}") from e

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}

            query = self._parse_query(args.get("query"))
            max_results = self._parse_max_results(args.get("max_results"))

            utils = self._utils()
            gmail_client = utils.get_gmail_client(None)
            hits = gmail_client.search_emails(query, max_results=max_results)

            rows: list[dict[str, Any]] = []
            for hit in hits:
                if not isinstance(hit, dict):
                    raise ValueError("Gmail search result entry must be an object.")
                message_id = str(hit.get("uid") or "").strip()
                if not message_id:
                    raise ValueError("Gmail search result entry missing uid.")
                thread_id = str(hit.get("threadId") or "").strip()

                full_email = gmail_client.fetch_full_email(message_id)
                if not full_email or not isinstance(full_email.get("raw_email"), str):
                    raise ValueError(f"Failed to fetch full email for message_id='{message_id}'.")

                email_message = message_from_string(full_email["raw_email"])
                metadata = EmailProcessor.extract_metadata(email_message)
                body = EmailProcessor.extract_email_body(email_message)

                date_received = str(metadata.get("date_received") or "")
                # HEADERS ONLY — never the full body. The agent picks interesting
                # rows from these, then hydrates the chosen one(s) via
                # get_email_thread(message_id). `meeting_link` is the single
                # body-derived signal we lift up so a join link is visible without
                # hydrating.
                row = {
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "subject": str(metadata.get("subject") or ""),
                    "sender": str(metadata.get("sender") or ""),
                    "email_address": str(metadata.get("email_address") or ""),
                    "date_received": date_received,
                    "date_received_utc_iso": self._to_utc_iso(date_received) if date_received else "",
                    "snippet": self._snippet(body),
                    "meeting_link": self._extract_meeting_link(body),
                }
                rows.append(row)

            # Headers-only summary: one compact block per message (subject, sender,
            # date, message_id, snippet, any meeting link). Full bodies are fetched
            # on demand via get_email_thread(message_id) — this keeps even a wide
            # (dozens-of-rows) match small enough that it never overflows the
            # manager's working context or defeats the summary compactor.
            header = (
                f"Fetched {len(rows)} message(s) matching query: {query!r}. "
                "To read a message's full body, call get_email_thread with its message_id."
            )
            summary_lines = [header]
            for idx, r in enumerate(rows, start=1):
                subject = str(r.get("subject") or "").strip() or "(no subject)"
                sender = str(r.get("sender") or r.get("email_address") or "").strip()
                date = str(r.get("date_received") or "").strip()
                head = f"[{idx}] {subject}"
                if sender:
                    head += f" — {sender}"
                if date:
                    head += f" @ {date}"
                head += f"  (id: {r.get('message_id')})"
                summary_lines.append(head)
                snippet = str(r.get("snippet") or "").strip()
                if snippet:
                    summary_lines.append(f"    {snippet}")
                link = str(r.get("meeting_link") or "").strip()
                if link:
                    summary_lines.append(f"    meeting link: {link}")
            summary = "\n".join(summary_lines)

            return ToolResult(
                result_type="get_email_messages",
                content=summary,
                data={
                    "query": query,
                    "max_results": max_results,
                    "message_count": len(rows),
                    "messages": rows,
                },
            )
        except Exception as e:
            logger.error("get_email_messages failed: %s", e)
            logger.debug("get_email_messages exception details", exc_info=True)
            return make_tool_error(
                error_code="get_email_messages_failed",
                message=f"get_email_messages failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "get_email_messages"},
            )


def get_tool_class():
    return GetEmailMessagesTool

