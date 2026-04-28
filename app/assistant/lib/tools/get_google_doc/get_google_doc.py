from __future__ import annotations

from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.google_docs_tool.google_docs_client import GoogleDocsClient
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_MAX_CHARS = 100_000


class GetGoogleDocTool(BaseTool):
    """
    Read a Google Docs document and return its plain-text content plus metadata.

    Supports reading by document_id or searching by name.  Returns the full
    plain-text body (truncated at max_chars if necessary), the document title,
    revision id, and Drive metadata (created/modified times, web link).
    """

    def __init__(self):
        super().__init__("get_google_doc")

    @staticmethod
    def _parse_max_chars(raw: Any) -> int:
        if raw is None:
            return 20_000
        try:
            v = int(raw)
        except Exception as e:
            logger.error("[GetGoogleDocTool] Invalid max_chars %r: %s", raw, e)
            logger.debug("[GetGoogleDocTool] max_chars parse exception details", exc_info=True)
            raise ValueError(f"max_chars must be an integer, got {raw!r}") from e
        if v < 1:
            raise ValueError("max_chars must be >= 1")
        return min(v, _MAX_CHARS)

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}

            document_id = str(args.get("document_id") or "").strip()
            name_search = str(args.get("name_search") or "").strip()
            account_id = str(args.get("account_id") or "").strip() or None
            include_body = bool(args.get("include_body", True))
            max_chars = self._parse_max_chars(args.get("max_chars"))

            if not document_id and not name_search:
                raise ValueError("Either document_id or name_search is required.")

            client = GoogleDocsClient(account_id=account_id, readonly=True)

            # If no direct ID, search by name first
            if not document_id:
                hits = client.find_documents(name_contains=name_search, max_results=5)
                if not hits:
                    return ToolResult(
                        result_type="get_google_doc",
                        content=f"No Google Doc found matching name '{name_search}'.",
                        data={"found": False, "name_search": name_search},
                    )
                # Use the most recently modified match
                document_id = str(hits[0].get("id") or "").strip()
                logger.info(
                    "[GetGoogleDocTool] name_search=%r resolved to document_id=%s",
                    name_search,
                    document_id,
                )

            doc = client.get_document(document_id)
            title = str(doc.get("title") or "")
            revision_id = str(doc.get("revisionId") or "")

            # Drive metadata (non-fatal if it fails)
            drive_meta: dict[str, Any] = {}
            try:
                drive_meta = client.get_document_metadata(document_id)
            except Exception as e:
                logger.warning(
                    "[GetGoogleDocTool] Could not fetch Drive metadata for %s: %s",
                    document_id,
                    e,
                )

            body_text = ""
            truncated = False
            if include_body:
                body_text = client.extract_plain_text(doc)
                if len(body_text) > max_chars:
                    body_text = body_text[:max_chars]
                    truncated = True

            char_count = len(client.extract_plain_text(doc)) if include_body else None

            result_data: dict[str, Any] = {
                "found": True,
                "document_id": document_id,
                "title": title,
                "revision_id": revision_id,
                "web_link": drive_meta.get("webViewLink", ""),
                "created_time": drive_meta.get("createdTime", ""),
                "modified_time": drive_meta.get("modifiedTime", ""),
                "owners": [
                    o.get("emailAddress", "") for o in (drive_meta.get("owners") or [])
                ],
                "char_count": char_count,
                "truncated": truncated,
                "max_chars": max_chars if include_body else None,
                "body": body_text if include_body else "",
            }

            summary = f"Document '{title}' (id={document_id})."
            if include_body:
                summary += f" {char_count} chars"
                if truncated:
                    summary += f" (truncated to {max_chars})"
                summary += "."

            return ToolResult(
                result_type="get_google_doc",
                content=summary,
                data=result_data,
            )

        except Exception as e:
            logger.error("[GetGoogleDocTool] failed: %s", e)
            logger.debug("[GetGoogleDocTool] exception details", exc_info=True)
            return make_tool_error(
                error_code="get_google_doc_failed",
                message=f"get_google_doc failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "get_google_doc"},
            )


def get_tool_class():
    return GetGoogleDocTool
