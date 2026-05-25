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

            # Render the full body of each message inline in `content`.
            # Earlier versions emitted only `snippet[:240]` per row plus a
            # 15000-char silent body slice in `data` — both invisible to
            # the planner via the metadata-only summary. When the user
            # asked for "the full email about X" the planner literally
            # could not see the body and would report back the snippet.
            # Same architectural shape as the http_request bug fixed
            # 2026-05-25: useful data trapped in `data`, only metadata in
            # `content`. Fix mirrors that one — full body inline, let the
            # manager's summary_pre_node (trigger_on_large_result_chars:
            # 5000) compact for downstream prompt budget if needed.
            header = f"Fetched {len(rows)} message(s) matching query: {query!r}."
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
                summary_lines.append(head)
                body_text = str(r.get("body") or "").strip()
                if body_text:
                    # Indent the body so the boundary between header and body
                    # is unambiguous when multiple messages are rendered.
                    indented = "\n".join("    " + line for line in body_text.split("\n"))
                    summary_lines.append(indented)
                else:
                    summary_lines.append("    (empty body)")
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

