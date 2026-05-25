from __future__ import annotations

from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.google_docs_tool.google_docs_client import GoogleDocsClient
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class GetGoogleDocTool(BaseTool):
    """
    Read a Google Docs document and return its plain-text content plus metadata.

    Supports reading by document_id or searching by name. Returns the full
    plain-text body, the document title, revision id, and Drive metadata
    (created/modified times, web link). The body is rendered inline in
    ``content`` so the calling planner can read it directly; long docs
    are compacted by the manager's summary_pre_node before reaching the
    next planner turn.
    """

    def __init__(self):
        super().__init__("get_google_doc")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}

            document_id = str(args.get("document_id") or "").strip()
            name_search = str(args.get("name_search") or "").strip()
            include_body = bool(args.get("include_body", True))

            if not document_id and not name_search:
                raise ValueError("Either document_id or name_search is required.")

            client = GoogleDocsClient(readonly=True)

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
            if include_body:
                body_text = client.extract_plain_text(doc)

            char_count = len(body_text) if include_body else None

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
                "body": body_text if include_body else "",
            }

            # Render the full doc body inline in `content` so the planner
            # can actually read it. Earlier behavior: silently sliced body
            # at 20K chars in `data["body"]` and emitted a metadata-only
            # summary in `content` — when the user asked for "the full
            # doc" the planner could only see a one-line header. Same
            # architectural defect class as the email tools fix
            # (2026-05-25). Manager's summary_pre_node compacts long docs
            # downstream if needed.
            header_lines = [f"Document '{title}' (id={document_id})"]
            if include_body and char_count is not None:
                header_lines[0] += f" — {char_count} chars"
            web_link = drive_meta.get("webViewLink", "")
            if web_link:
                header_lines.append(f"web_link: {web_link}")
            if include_body:
                header_lines.append("body:")
                if body_text:
                    header_lines.append("\n".join("    " + line for line in body_text.split("\n")))
                else:
                    header_lines.append("    (empty)")

            return ToolResult(
                result_type="get_google_doc",
                content="\n".join(header_lines),
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
