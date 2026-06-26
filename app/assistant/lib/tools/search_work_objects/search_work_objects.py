"""search_work_objects — keyword search over the dayflow work-object store (titles + step contents) so
an agent can find prior or ongoing work to answer questions about or reuse. Read-only. Returns matching
work objects (id, title, status); follow with read_work_object for the full detail.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)
_DEFAULT_LIMIT = 10


class SearchWorkObjectsTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("search_work_objects")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            args = CreateDayflowTicketTool._extract_arguments(tool_message)
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("query is required")
            try:
                limit = int(args.get("limit") or _DEFAULT_LIMIT)
            except Exception:
                limit = _DEFAULT_LIMIT
            hits = self._search(query, max(1, limit))
            if not hits:
                return ToolResult(result_type="success",
                                  content=f"No work objects match {query!r}.", data={"results": []})
            lines = [f"{len(hits)} work object(s) matching {query!r} (newest first):"]
            for h in hits:
                lines.append(f"- {h['work_id']}  [{h['status']}]  {h['title']}")
            lines.append("\nUse read_work_object(<work_id>) to read one in full.")
            return ToolResult(result_type="success", content="\n".join(lines), data={"results": hits})
        except Exception as e:
            logger.error("search_work_objects failed: %s", e)
            logger.debug("search_work_objects exception", exc_info=True)
            return ToolResult(result_type="error", content=f"search_work_objects failed: {e}", data={})

    @staticmethod
    def _search(query: str, limit: int):
        """Match ALL whitespace-separated terms (AND) against title + concatenated node contents."""
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        conn = get_dayflow_work_store()._conn
        blobs = {}
        for wid, title, status, updated in conn.execute(
                "SELECT id, title, status, updated_at FROM work_objects"):
            blobs[wid] = {"work_id": wid, "title": title or "", "status": status or "",
                          "updated_at": updated or "", "_text": (title or "").lower()}
        for wid, content in conn.execute("SELECT work_id, content FROM nodes WHERE content IS NOT NULL"):
            if wid in blobs and content:
                blobs[wid]["_text"] += " " + str(content).lower()
        terms = [t for t in query.lower().split() if t]
        hits = [d for d in blobs.values() if all(t in d["_text"] for t in terms)]
        hits.sort(key=lambda d: d["updated_at"], reverse=True)
        for d in hits:
            d.pop("_text", None)
        return hits[:limit]


def get_tool_class():
    return SearchWorkObjectsTool
