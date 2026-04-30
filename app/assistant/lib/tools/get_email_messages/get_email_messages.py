from __future__ import annotations

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
    Full-fidelity email retrieval by Gmail query.

    Unlike get_email/fetch_email, this tool is not triage-summarized and does not
    apply importance filtering. It returns all matched messages (subject to max_results),
    including parsed bodies when requested.
    """

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
        return min(value, 200)

    @staticmethod
    def _parse_body_max_chars(raw: Any) -> int:
        if raw is None:
            return 15000
        try:
            value = int(raw)
        except Exception as e:
            logger.error("Invalid body_max_chars value: %r", raw)
            logger.debug("get_email_messages body_max_chars parse exception details", exc_info=True)
            raise ValueError(f"body_max_chars must be an integer, got {raw!r}") from e
        if value < 1:
            raise ValueError("body_max_chars must be >= 1")
        return min(value, 200000)

    @staticmethod
    def _header_map(email_message: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            for key, value in (email_message.items() if hasattr(email_message, "items") else []):
                norm_key = str(key or "").strip().lower()
                if not norm_key:
                    continue
                out[norm_key] = str(value or "").strip()
        except Exception:
            logger.error("Failed to collect message headers.")
            logger.debug("get_email_messages header map exception details", exc_info=True)
            raise
        return out

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
            include_body = bool(args.get("include_body", True))
            include_headers = bool(args.get("include_headers", True))
            body_max_chars = self._parse_body_max_chars(args.get("body_max_chars")) if include_body else 0

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
                headers = self._header_map(email_message) if include_headers else {}
                body = EmailProcessor.extract_email_body(email_message) if include_body else ""
                if include_body and len(body) > body_max_chars:
                    body = body[:body_max_chars]

                date_received = str(metadata.get("date_received") or "")
                row = {
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "subject": str(metadata.get("subject") or ""),
                    "sender": str(metadata.get("sender") or ""),
                    "email_address": str(metadata.get("email_address") or ""),
                    "date_received": date_received,
                    "date_received_utc_iso": self._to_utc_iso(date_received) if date_received else "",
                    "snippet": body[:240] if body else "",
                    "body": body if include_body else "",
                    "headers": headers if include_headers else {},
                    "internet_message_id": str(headers.get("message-id") or ""),
                    "in_reply_to": str(headers.get("in-reply-to") or ""),
                    "references": str(headers.get("references") or ""),
                }
                rows.append(row)

            # Build a per-message summary so the calling agent can actually
            # read subjects, senders, dates, and body snippets inline.
            # Previously `content` was just a count — callers had no way to
            # see who sent what, and retried queries hoping for different
            # output. Include enough per-row to answer the common questions
            # ("which email has the Zoom link?", "is any email from X?").
            header = f"Fetched {len(rows)} message(s) matching query: {query!r}."
            summary_lines = [header]
            for idx, r in enumerate(rows, start=1):
                subject = str(r.get("subject") or "").strip() or "(no subject)"
                sender = str(r.get("sender") or r.get("email_address") or "").strip()
                date = str(r.get("date_received") or "").strip()
                snippet = str(r.get("snippet") or "").strip()
                line = f"[{idx}] {subject}"
                if sender:
                    line += f" — {sender}"
                if date:
                    line += f" @ {date}"
                if snippet:
                    line += f"\n    snippet: {snippet[:240]}"
                # Also surface any obvious conference link hiding in the
                # body so the planner doesn't need another round-trip.
                body_text = str(r.get("body") or "")
                if body_text:
                    for keyword in ("zoom.us/j/", "meet.google.com/", "teams.microsoft.com/"):
                        pos = body_text.find(keyword)
                        if pos >= 0:
                            window = body_text[pos:pos + 300].replace("\n", " ")
                            line += f"\n    link: {window[:300]}"
                            break
                summary_lines.append(line)
            summary = "\n".join(summary_lines)

            return ToolResult(
                result_type="get_email_messages",
                content=summary,
                data={
                    "query": query,
                    "max_results": max_results,
                    "message_count": len(rows),
                    "messages": rows,
                    "include_body": include_body,
                    "include_headers": include_headers,
                    "body_max_chars": body_max_chars if include_body else 0,
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

