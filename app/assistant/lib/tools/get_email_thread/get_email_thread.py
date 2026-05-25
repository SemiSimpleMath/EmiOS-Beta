from __future__ import annotations

from email import message_from_string
from datetime import datetime, timedelta, timezone
from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.core_tools.email_tool.utils.email_utils import EmailUtils
from app.assistant.lib.core_tools.email_tool.utils.email_processor import EmailProcessor
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class GetEmailThreadTool(BaseTool):
    """
    Fetch a full Gmail conversation thread in chronological order.
    """

    def __init__(self):
        super().__init__("get_email_thread")
        self._email_utils: EmailUtils | None = None

    def _utils(self) -> EmailUtils:
        if self._email_utils is None:
            self._email_utils = EmailUtils()
        return self._email_utils

    @staticmethod
    def _parse_max_messages(raw: Any) -> int:
        if raw is None:
            return 100
        try:
            value = int(raw)
        except Exception as e:
            logger.error("Invalid max_messages value: %r", raw)
            logger.debug("get_email_thread max_messages parse exception details", exc_info=True)
            raise ValueError(f"max_messages must be an integer, got {raw!r}") from e
        if value < 1:
            raise ValueError("max_messages must be >= 1")
        return min(value, 500)

    @staticmethod
    def _parse_recent_message_limit(raw: Any) -> int:
        if raw is None:
            return 8
        try:
            value = int(raw)
        except Exception as e:
            logger.error("Invalid recent_message_limit value: %r", raw)
            logger.debug("get_email_thread recent_message_limit parse exception details", exc_info=True)
            raise ValueError(f"recent_message_limit must be an integer, got {raw!r}") from e
        if value < 1:
            raise ValueError("recent_message_limit must be >= 1")
        return min(value, 50)

    @staticmethod
    def _parse_lookback_hours(raw: Any) -> int | None:
        if raw is None:
            return None
        try:
            value = int(raw)
        except Exception as e:
            logger.error("Invalid lookback_hours value: %r", raw)
            logger.debug("get_email_thread lookback_hours parse exception details", exc_info=True)
            raise ValueError(f"lookback_hours must be an integer, got {raw!r}") from e
        if value < 1:
            raise ValueError("lookback_hours must be >= 1")
        return min(value, 24 * 30)

    @staticmethod
    def _extract_recent_message_row(
        *,
        gmail_client: Any,
        message_id: str,
        fallback_thread_id: str,
        include_body: bool,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "message_id": message_id,
            "thread_id": fallback_thread_id,
            "subject": "",
            "date_received": "",
            "sender": "",
            "email_address": "",
            "snippet": "",
            "body": "",
        }
        if not message_id:
            return row
        full_email = gmail_client.fetch_full_email(message_id)
        if not full_email or not isinstance(full_email.get("raw_email"), str):
            return row
        try:
            email_message = message_from_string(full_email["raw_email"])
            metadata = EmailProcessor.extract_metadata(email_message)
            row["subject"] = str(metadata.get("subject") or "")
            row["date_received"] = str(metadata.get("date_received") or "")
            row["sender"] = str(metadata.get("sender") or "")
            row["email_address"] = str(metadata.get("email_address") or "")
            if include_body:
                row["body"] = EmailProcessor.extract_email_body(email_message)
        except Exception as e:
            logger.error("Failed parsing recent email message_id=%s: %s", message_id, e)
            logger.debug("get_email_thread recent message parse exception details", exc_info=True)
        return row

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}
            thread_id = str(args.get("thread_id") or "").strip()
            seed_message_id = str(args.get("seed_message_id") or "").strip()
            participant_email = str(args.get("participant_email") or "").strip()
            include_body = bool(args.get("include_body", True))
            max_messages = self._parse_max_messages(args.get("max_messages"))
            include_recent_messages = bool(args.get("include_recent_messages", True))
            recent_message_limit = self._parse_recent_message_limit(args.get("recent_message_limit"))
            lookback_hours = self._parse_lookback_hours(args.get("lookback_hours"))

            utils = self._utils()
            gmail_client = utils.get_gmail_client(None)
            inbound_hits: list[dict[str, Any]] = []
            bilateral_hits: list[dict[str, Any]] = []
            if not thread_id and seed_message_id:
                thread_id = gmail_client.get_thread_id_for_message(seed_message_id)
            if not thread_id and participant_email:
                # Prefer inbound participant mail first; fallback to bilateral conversation hits.
                lookback_clause = ""
                if lookback_hours is not None:
                    after_epoch = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
                    lookback_clause = f" after:{after_epoch}"
                inbound_query = f"from:{participant_email}{lookback_clause}"
                bilateral_query = f"(from:{participant_email} OR to:{participant_email}){lookback_clause}"
                inbound_hits = gmail_client.search_emails(inbound_query, max_results=recent_message_limit)
                bilateral_hits = gmail_client.search_emails(bilateral_query, max_results=recent_message_limit)
                chosen_hits = inbound_hits if inbound_hits else bilateral_hits
                for hit in chosen_hits:
                    if not isinstance(hit, dict):
                        continue
                    candidate = str(hit.get("threadId") or "").strip()
                    if candidate:
                        thread_id = candidate
                        break
            if not thread_id and not seed_message_id and not participant_email:
                return make_tool_error(
                    error_code="get_email_thread_missing_identifier",
                    message="get_email_thread requires thread_id, seed_message_id, or participant_email.",
                    abort_policy="abort_tool",
                    retryable=False,
                    details={"tool": "get_email_thread"},
                )
            if not thread_id:
                # Canonical no-match behavior for participant lookup: return empty thread, not error.
                return ToolResult(
                    result_type="get_email_thread",
                    content="No matching thread found for provided lookup arguments.",
                    data={
                        "thread_id": "",
                        "message_count": 0,
                        "messages": [],
                        "thread_found": False,
                        "lookup": {
                            "thread_id": thread_id,
                            "seed_message_id": seed_message_id,
                            "participant_email": participant_email,
                        },
                        "recent_messages": [],
                        "recent_message_count": 0,
                        "recent_thread_ids": [],
                    },
                )

            messages = utils.fetch_full_thread(
                thread_id=thread_id,
                max_messages=max_messages,
                include_body=include_body,
            )
            recent_messages: list[dict[str, Any]] = []
            recent_thread_ids: list[str] = []
            if include_recent_messages and participant_email:
                lookback_clause = ""
                if lookback_hours is not None:
                    after_epoch = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
                    lookback_clause = f" after:{after_epoch}"
                if not inbound_hits:
                    inbound_hits = gmail_client.search_emails(
                        f"from:{participant_email}{lookback_clause}",
                        max_results=recent_message_limit,
                    )
                if not bilateral_hits:
                    bilateral_hits = gmail_client.search_emails(
                        f"(from:{participant_email} OR to:{participant_email}){lookback_clause}",
                        max_results=recent_message_limit,
                    )
                seen_message_ids: set[str] = set()
                combined_hits = [*inbound_hits, *bilateral_hits]
                for hit in combined_hits:
                    if not isinstance(hit, dict):
                        continue
                    message_id = str(hit.get("uid") or "").strip()
                    if not message_id or message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(message_id)
                    candidate_thread_id = str(hit.get("threadId") or "").strip()
                    if candidate_thread_id and candidate_thread_id not in recent_thread_ids:
                        recent_thread_ids.append(candidate_thread_id)
                    recent_messages.append(
                        self._extract_recent_message_row(
                            gmail_client=gmail_client,
                            message_id=message_id,
                            fallback_thread_id=candidate_thread_id,
                            include_body=include_body,
                        )
                    )
                    if len(recent_messages) >= recent_message_limit:
                        break
            # Render the full body of each message inline in `content` so
            # the calling planner can actually read the thread. The
            # earlier "Fetched N message(s) from thread X." line left the
            # full conversation in `data["messages"]` — invisible to the
            # planner. Same architectural shape as the get_email_messages
            # bug fixed alongside this (2026-05-25). Per-message header
            # (subject + sender + date) with indented body keeps message
            # boundaries unambiguous; the manager's summary_pre_node
            # (trigger_on_large_result_chars: 5000) handles compaction
            # for long threads.
            summary_lines = [f"Thread {thread_id} — {len(messages)} message(s) in chronological order:"]
            for idx, m in enumerate(messages, start=1):
                if not isinstance(m, dict):
                    continue
                subject = str(m.get("subject") or "").strip() or "(no subject)"
                sender = str(m.get("sender") or m.get("email_address") or "").strip()
                date = str(m.get("date_received") or "").strip()
                head = f"[{idx}] {subject}"
                if sender:
                    head += f" — {sender}"
                if date:
                    head += f" @ {date}"
                summary_lines.append(head)
                body_text = str(m.get("body") or "").strip()
                if body_text:
                    summary_lines.append("\n".join("    " + line for line in body_text.split("\n")))
                else:
                    summary_lines.append("    (empty body)")
            content = "\n".join(summary_lines)

            return ToolResult(
                result_type="get_email_thread",
                content=content,
                data={
                    "thread_id": thread_id,
                    "message_count": len(messages),
                    "messages": messages,
                    "thread_found": True,
                    "recent_messages": recent_messages,
                    "recent_message_count": len(recent_messages),
                    "recent_thread_ids": recent_thread_ids,
                    "selected_thread_matches_latest_recent": bool(
                        recent_thread_ids and recent_thread_ids[0] == thread_id
                    ),
                    "lookback_hours": lookback_hours,
                },
            )
        except Exception as e:
            logger.error("get_email_thread failed: %s", e)
            logger.debug("get_email_thread exception details", exc_info=True)
            return make_tool_error(
                error_code="get_email_thread_failed",
                message=f"get_email_thread failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "get_email_thread"},
            )


def get_tool_class():
    return GetEmailThreadTool
