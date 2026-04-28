from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.email_tool.utils.email_utils import EmailUtils
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_MAX_LOOKBACK = timedelta(days=7)
_FUTURE_SKEW_FALLBACK = timedelta(hours=1)


class GetImportantEmailsTool(BaseTool):
    """
    Summarized inbox email triage tool.

    Fetches inbox mail for a time window, runs each message through the
    email_parser agent, and returns only messages with importance >= 5.
    Each result includes LLM-generated summary and action_items.
    Nothing is written to the EventRepository or any file — results are
    ephemeral and intended for the calling agent's context only.

    For large-scale or query-driven searches, use get_email_messages.
    For full thread reconstruction, use get_email_thread.
    """

    def __init__(self):
        super().__init__("get_important_emails")
        self._email_utils: EmailUtils | None = None

    def _utils(self) -> EmailUtils:
        if self._email_utils is None:
            self._email_utils = EmailUtils()
        return self._email_utils

    @staticmethod
    def _parse_datetime(raw: Any, label: str) -> datetime:
        if not raw:
            raise ValueError(f"{label} is required.")
        iso_str = str(raw).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_str)
        except Exception as e:
            logger.error("[GetImportantEmailsTool] Cannot parse %s=%r: %s", label, raw, e)
            logger.debug("[GetImportantEmailsTool] %s parse exception details", label, exc_info=True)
            raise ValueError(f"Invalid {label} format: {raw!r}") from e
        if dt.tzinfo is None:
            logger.warning("[GetImportantEmailsTool] %s was naive; treating as UTC.", label)
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}

            raw_start = args.get("start_date")
            raw_end = args.get("end_date")
            unseen = bool(args.get("unseen", False))
            account_id = str(args.get("account_id") or "").strip() or None

            now_utc = datetime.now(timezone.utc)

            # --- start_date ---
            if not raw_start:
                start_dt = now_utc - timedelta(hours=12)
                logger.info("[GetImportantEmailsTool] No start_date; defaulting to 12 hours ago.")
            else:
                start_dt = self._parse_datetime(raw_start, "start_date")
                if start_dt > now_utc:
                    logger.warning(
                        "[GetImportantEmailsTool] start_date %s is in the future; adjusting to %s ago.",
                        start_dt,
                        _FUTURE_SKEW_FALLBACK,
                    )
                    start_dt = now_utc - _FUTURE_SKEW_FALLBACK
                if now_utc - start_dt > _MAX_LOOKBACK:
                    logger.warning(
                        "[GetImportantEmailsTool] start_date older than %d days; capping.",
                        _MAX_LOOKBACK.days,
                    )
                    start_dt = now_utc - _MAX_LOOKBACK

            # --- end_date (optional) ---
            end_dt: datetime | None = None
            if raw_end:
                end_dt = self._parse_datetime(raw_end, "end_date")

            logger.info(
                "[GetImportantEmailsTool] window start=%s end=%s unseen=%s account_id=%s",
                start_dt.isoformat(),
                end_dt.isoformat() if end_dt else "now",
                unseen,
                account_id,
            )

            emails = self._utils().fetch_and_filter_emails(
                start_timestamp=start_dt,
                end_timestamp=end_dt,
                unseen=unseen,
                account_id=account_id,
            )

            return ToolResult(
                result_type="get_important_emails",
                content=(
                    f"Fetched {len(emails)} relevant email(s) for the requested window. "
                    "Results are importance-filtered and LLM-summarized. "
                    "Use get_email_thread for full thread/body retrieval."
                ),
                data_list=emails,
            )
        except Exception as e:
            logger.error("[GetImportantEmailsTool] failed: %s", e)
            logger.debug("[GetImportantEmailsTool] exception details", exc_info=True)
            return make_tool_error(
                error_code="get_important_emails_failed",
                message=f"get_important_emails failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "get_important_emails"},
            )


def get_tool_class():
    return GetImportantEmailsTool
