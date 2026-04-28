from __future__ import annotations

from typing import Any

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.google_docs_tool.google_docs_client import GoogleDocsClient
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_OPERATIONS = {"append", "diff"}

# How many characters to show around each ambiguous match site
_CONTEXT_CHARS = 200


def _find_all(text: str, needle: str) -> list[int]:
    """Return start offsets of every non-overlapping occurrence of needle in text."""
    positions: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + len(needle)
    return positions


def _excerpt(text: str, offset: int, length: int, context: int) -> str:
    """Return up to context chars before and after [offset, offset+length)."""
    lo = max(0, offset - context)
    hi = min(len(text), offset + length + context)
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return prefix + text[lo:hi] + suffix


class EditGoogleDocTool(BaseTool):
    """
    Edit a Google Docs document using a diff-style approach.

    Operations:
      diff    — The primary edit operation. Provide a search block (find) and a
                replacement block (replace_with). The tool reads the document,
                finds all occurrences of the search block, and:
                  - Exactly 1 match → applies the replacement and returns success.
                  - 0 matches       → returns an error with suggested diagnostic info.
                  - 2+ matches      → returns an AMBIGUOUS error with expanded context
                                      around each match site so the caller can widen
                                      the search block to make it unique, then retry.

      append  — Append text to the end of the document (no search needed).

    The diff workflow mirrors how coding agents make edits:
      1. Call get_google_doc to read the current document text.
      2. Identify the exact text block to replace (find), including enough surrounding
         lines to make it unique in the document.
      3. Call edit_google_doc with operation=diff, providing find and replace_with.
         replace_with should be the full replacement — not just the new lines, but
         any existing lines you want to keep too.
      4. If AMBIGUOUS is returned, widen the find block using the context excerpts
         provided, then retry.
    """

    def __init__(self):
        super().__init__("edit_google_doc")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {}

            document_id = str(args.get("document_id") or "").strip()
            if not document_id:
                raise ValueError("document_id is required.")

            operation = str(args.get("operation") or "").strip().lower()
            if not operation:
                raise ValueError(f"operation is required. One of: {sorted(_OPERATIONS)}.")
            if operation not in _OPERATIONS:
                raise ValueError(
                    f"Unknown operation '{operation}'. Must be one of: {sorted(_OPERATIONS)}."
                )

            account_id = str(args.get("account_id") or "").strip() or None
            client = GoogleDocsClient(account_id=account_id, readonly=False)

            # ------------------------------------------------------------------
            # APPEND
            # ------------------------------------------------------------------
            if operation == "append":
                text = str(args.get("text") or "")
                if not text:
                    raise ValueError("text is required for operation=append.")
                ensure_newline = bool(args.get("ensure_newline", True))
                resp = client.append_text(document_id, text, ensure_newline=ensure_newline)
                return ToolResult(
                    result_type="edit_google_doc",
                    content=f"Appended {len(text)} chars to document {document_id}.",
                    data=self._base_data(resp, operation, document_id),
                )

            # ------------------------------------------------------------------
            # DIFF
            # ------------------------------------------------------------------
            if operation == "diff":
                find = str(args.get("find") or "")
                if not find:
                    raise ValueError(
                        "find is required for operation=diff. "
                        "Provide the exact text block to replace, with enough surrounding "
                        "context to uniquely identify the location."
                    )
                replace_with = str(args.get("replace_with") if args.get("replace_with") is not None else "")

                # Read the current document text to locate matches ourselves.
                doc = client.get_document(document_id)
                full_text = client.extract_plain_text(doc)

                positions = _find_all(full_text, find)
                match_count = len(positions)

                logger.info(
                    "[EditGoogleDocTool] diff document_id=%s find_len=%d match_count=%d",
                    document_id,
                    len(find),
                    match_count,
                )

                # --- 0 matches ---
                if match_count == 0:
                    return make_tool_error(
                        error_code="edit_google_doc_no_match",
                        message=(
                            "The search block was not found in the document. "
                            "Call get_google_doc to re-read the current document text, "
                            "then adjust the find block to match the exact current content."
                        ),
                        abort_policy="abort_tool",
                        retryable=True,
                        details={
                            "tool": "edit_google_doc",
                            "document_id": document_id,
                            "find_preview": find[:120],
                        },
                    )

                # --- 2+ matches: ambiguity ---
                if match_count > 1:
                    sites = []
                    for pos in positions:
                        sites.append({
                            "offset": pos,
                            "context": _excerpt(full_text, pos, len(find), _CONTEXT_CHARS),
                        })
                    return make_tool_error(
                        error_code="edit_google_doc_ambiguous",
                        message=(
                            f"The search block matched {match_count} locations in the document. "
                            "The find block must uniquely identify one location. "
                            "Expand it by including more surrounding lines from the context "
                            "excerpts below until the extended block is unique, then retry."
                        ),
                        abort_policy="abort_tool",
                        retryable=True,
                        details={
                            "tool": "edit_google_doc",
                            "document_id": document_id,
                            "match_count": match_count,
                            "match_sites": sites,
                            "find_preview": find[:120],
                        },
                    )

                # --- exactly 1 match: apply via replace_all_text ---
                resp = client.replace_all_text(
                    document_id,
                    find=find,
                    replace_with=replace_with,
                    match_case=True,
                )
                occurrences = self._occurrences_changed(resp)
                # Sanity: the API should report exactly 1 replacement
                if occurrences != 1:
                    logger.error(
                        "[EditGoogleDocTool] diff expected 1 replacement, API reported %d "
                        "for document_id=%s",
                        occurrences,
                        document_id,
                    )
                    raise RuntimeError(
                        f"Unexpected replacement count from Google Docs API: "
                        f"expected 1, got {occurrences}. The document may have changed "
                        "between the read and the write. Re-read and retry."
                    )

                return ToolResult(
                    result_type="edit_google_doc",
                    content=(
                        f"Applied diff to document {document_id}. "
                        f"Replaced {len(find)} chars with {len(replace_with)} chars."
                    ),
                    data={
                        **self._base_data(resp, operation, document_id),
                        "find_len": len(find),
                        "replace_with_len": len(replace_with),
                        "occurrences_changed": occurrences,
                    },
                )

            raise ValueError(f"Unhandled operation: {operation}")

        except Exception as e:
            logger.error("[EditGoogleDocTool] failed: %s", e)
            logger.debug("[EditGoogleDocTool] exception details", exc_info=True)
            return make_tool_error(
                error_code="edit_google_doc_failed",
                message=f"edit_google_doc failed: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "edit_google_doc"},
            )

    @staticmethod
    def _base_data(
        resp: dict[str, Any],
        operation: str,
        document_id: str,
    ) -> dict[str, Any]:
        write_control = resp.get("writeControl") or {}
        return {
            "document_id": document_id,
            "operation": operation,
            "revision_id": str(
                write_control.get("requiredRevisionId")
                or write_control.get("targetRevisionId")
                or ""
            ),
        }

    @staticmethod
    def _occurrences_changed(resp: dict[str, Any]) -> int:
        for reply in (resp.get("replies") or []):
            if isinstance(reply, dict):
                rat = reply.get("replaceAllText")
                if isinstance(rat, dict):
                    return int(rat.get("occurrencesChanged") or 0)
        return 0


def get_tool_class():
    return EditGoogleDocTool
