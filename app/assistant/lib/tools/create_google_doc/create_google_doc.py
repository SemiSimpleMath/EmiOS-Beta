from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.google_docs_tool.google_docs_client import GoogleDocsClient
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class CreateGoogleDocTool(BaseTool):
    """
    Create a new blank Google Docs document.

    Returns the document_id, title, and web link.  The caller can then
    use edit_google_doc to populate the document with content.
    """

    def __init__(self):
        super().__init__("create_google_doc")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}

            title = str(args.get("title") or "").strip()
            if not title:
                raise ValueError("title is required.")

            account_id = str(args.get("account_id") or "").strip() or None
            initial_content = str(args.get("initial_content") or "").strip()

            client = GoogleDocsClient(account_id=account_id, readonly=False)
            result = client.create_document(title)

            document_id = result["document_id"]
            web_view_link = result["web_view_link"]

            if initial_content:
                client.append_text(document_id, initial_content, ensure_newline=False)

            return ToolResult(
                result_type="create_google_doc",
                content=f"Created Google Doc '{title}' — {web_view_link}",
                data={
                    "document_id": document_id,
                    "title": result["title"],
                    "web_view_link": web_view_link,
                    "initial_content_written": bool(initial_content),
                },
            )

        except Exception as e:
            logger.error("[CreateGoogleDocTool] failed: %s", e)
            logger.debug("[CreateGoogleDocTool] exception details", exc_info=True)
            return make_tool_error(
                error_code="create_google_doc_failed",
                message=f"create_google_doc failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "create_google_doc"},
            )


def get_tool_class():
    return CreateGoogleDocTool
