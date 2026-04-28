from __future__ import annotations

from urllib.parse import urlsplit

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.scrape_url.utils import scrape
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


def _is_reddit_url(url: str) -> bool:
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        if host == "redd.it":
            return True
        if host == "reddit.com" or host.endswith(".reddit.com"):
            return True
        return False
    except Exception as e:
        logger.error("Failed to parse URL in peak_at_link reddit policy check: %s", e)
        logger.debug("peak_at_link reddit policy check exception details", exc_info=True)
        return False


class PeakAtLinkTool(BaseTool):
    """
    Quick read-only link peek:
    fetch page text and return first few human-readable lines.
    """

    def __init__(self):
        super().__init__("peak_at_link")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        arguments = tool_message.tool_data.get("arguments", {}) if tool_message.tool_data else {}
        url = str(arguments.get("url") or "").strip()
        max_lines = int(arguments.get("max_lines") or 8)
        max_chars_per_line = int(arguments.get("max_chars_per_line") or 220)
        max_lines = max(1, min(max_lines, 30))
        max_chars_per_line = max(60, min(max_chars_per_line, 400))

        if not url:
            return ToolResult(result_type="error", content="Missing required argument: url")

        if _is_reddit_url(url):
            return ToolResult(
                result_type="error",
                content="Refused to peek Reddit by policy.",
                data={"url": url, "policy": "no_reddit_scraping"},
            )

        try:
            result = scrape.scrape_page(url)
            page_text = result.get("full_text", "") if isinstance(result, dict) else ""
            if not page_text.strip():
                return ToolResult(
                    result_type="error",
                    content="Failed to read meaningful page text.",
                    data={"url": url},
                )

            raw_lines = [line.strip() for line in page_text.splitlines() if line and line.strip()]
            if not raw_lines:
                raw_lines = [segment.strip() for segment in page_text.split(". ") if segment.strip()]

            selected = raw_lines[:max_lines]
            preview_lines = [line[:max_chars_per_line] for line in selected]
            rendered_preview = "\n".join(f"- {line}" for line in preview_lines)

            return ToolResult(
                result_type="link_preview",
                content=f"Preview of {url}:\n{rendered_preview}",
                data={
                    "url": url,
                    "title": result.get("title", "") if isinstance(result, dict) else "",
                    "preview_lines": preview_lines,
                    "line_count": len(preview_lines),
                },
            )
        except Exception as e:
            logger.error("peak_at_link failed for url=%s: %s", url, e)
            logger.debug("peak_at_link execution exception details", exc_info=True)
            return ToolResult(
                result_type="error",
                content=f"peak_at_link failed: {e}",
                data={"url": url},
            )


def get_tool_class():
    return PeakAtLinkTool
