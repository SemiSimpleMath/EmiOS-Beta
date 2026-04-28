from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.email_tool.utils.gmail_api_client import GmailAPIClient
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_MAX_PER_CALL = 1000


class TrashEmailsTool(BaseTool):
    """
    Move all emails from one or more senders to Trash.

    Uses the Gmail batchModify endpoint — fast even for large inboxes.
    Supports an optional extra_query clause to narrow the scope
    (e.g. only trash messages older than a certain date).

    This is a destructive operation and requires user approval.
    """

    requires_approval = True

    def __init__(self):
        super().__init__("trash_emails")

    def describe_action(self, tool_message: ToolMessage) -> tuple:
        args = tool_message.tool_data.get("arguments") or {}
        senders = args.get("senders") or []
        extra = args.get("extra_query") or ""
        sender_str = ", ".join(senders) if senders else "(none)"
        msg = f"Trash all emails from: {sender_str}"
        if extra:
            msg += f"\nAdditional filter: {extra}"
        return "Trash emails by sender?", msg

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            args = tool_message.tool_data.get("arguments") if isinstance(tool_message.tool_data, dict) else {}
            if not isinstance(args, dict):
                args = {}

            senders = args.get("senders") or []
            if isinstance(senders, str):
                senders = [senders]
            senders = [str(s).strip() for s in senders if str(s).strip()]

            if not senders:
                raise ValueError("senders is required and must be a non-empty list of email addresses.")

            extra_query = str(args.get("extra_query") or "").strip()
            # Product decision: always trash at the hard per-sender max for this tool.
            # Ignore caller-provided max_per_sender to avoid inconsistent caps across runs.
            max_per_sender = _MAX_PER_CALL
            account_id = str(args.get("account_id") or "").strip() or None

            client = GmailAPIClient(account_id=account_id)

            results = []
            total_trashed = 0
            for sender in senders:
                outcome = client.batch_trash_by_sender(
                    sender_email=sender,
                    extra_query=extra_query,
                    max_results=max_per_sender,
                )
                trashed = outcome.get("trashed", 0)
                total_trashed += trashed
                results.append({"sender": sender, "trashed": trashed})
                logger.info("trash_emails: trashed %d message(s) from %s", trashed, sender)

            summary = f"Trashed {total_trashed} email(s) across {len(senders)} sender(s)."
            return ToolResult(
                result_type="trash_emails",
                content=summary,
                data={"total_trashed": total_trashed, "results": results},
            )

        except Exception as e:
            logger.error("[TrashEmailsTool] failed: %s", e)
            logger.debug("[TrashEmailsTool] exception details", exc_info=True)
            return make_tool_error(
                error_code="trash_emails_failed",
                message=f"trash_emails failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "trash_emails"},
            )


def get_tool_class():
    return TrashEmailsTool
