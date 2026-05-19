"""
web_get_content — extract readable text content from the current browser page.

Use this for reading and research, not navigation. Returns the page's visible
text content, cleaned of scripts/styles/nav noise. Ideal for extracting
headlines, article text, product descriptions, etc. from the page already
loaded in the playwright browser.
"""
from __future__ import annotations

import json
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class WebGetContent(BaseTool):
    """Extract readable text content from the current browser page."""

    SERVER_ID = "npm/playwright-mcp"
    # playwright-mcp 2026 upgrade: browser_run_code → browser_evaluate
    # (runs JS in page context, no Playwright `page` object available).
    MCP_EVALUATE = "browser_evaluate"

    # JS that extracts clean text from the page body, removing scripts,
    # styles, nav, footer, and other non-content elements.
    _EXTRACT_JS = """
() => {
  // Remove noise elements before extracting text.
  const removeSelectors = [
    'script', 'style', 'noscript', 'iframe',
    'nav', 'footer', 'header',
    '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
    '[aria-hidden="true"]',
    '.ad', '.advertisement', '.sponsored',
  ];
  const clone = document.body.cloneNode(true);
  for (const sel of removeSelectors) {
    for (const el of clone.querySelectorAll(sel)) {
      el.remove();
    }
  }

  // Extract text, collapse whitespace, limit length.
  const raw = clone.innerText || clone.textContent || '';
  const lines = raw.split('\\n')
    .map(l => l.trim())
    .filter(l => l.length > 0);
  const text = lines.join('\\n');

  return {
    url: window.location.href,
    title: document.title || '',
    text: text.slice(0, MAX_CHARS),
    truncated: text.length > MAX_CHARS,
    char_count: text.length,
    line_count: lines.length,
  };
}
""".strip()

    def __init__(self):
        super().__init__("web_get_content")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        max_chars = int(args.get("max_chars", 15000))
        # Clamp to reasonable range.
        max_chars = max(1000, min(max_chars, 50000))

        server_entry = None
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_get_content error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        # Inject max_chars into the JS.
        js = self._EXTRACT_JS.replace("MAX_CHARS", str(max_chars))

        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": js},
            timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
        )
        text_out, is_error, _ = format_mcp_tool_result_content(call_resp)

        if is_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_get_content error: browser_evaluate failed: {text_out}",
                abort_policy="abort_tool",
                retryable=True,
                details={"backend": "mcp", "server_id": self.SERVER_ID},
            )

        # Parse the result JSON from the MCP response.
        result_data = self._parse_result(text_out)

        url = result_data.get("url", "")
        title = result_data.get("title", "")
        page_text = result_data.get("text", "")
        truncated = result_data.get("truncated", False)
        line_count = result_data.get("line_count", 0)

        content_parts = [
            f"web_get_content:",
            f"- url: {url}",
            f"- title: {title}",
            f"- lines: {line_count}",
            f"- truncated: {truncated}",
            f"",
            page_text,
        ]

        logger.info(
            "web_get_content: url=%s title=%s lines=%d truncated=%s",
            url, title[:60], line_count, truncated,
        )

        return ToolResult(
            result_type="web_get_content",
            content="\n".join(content_parts),
            data=result_data,
        )

    def _parse_result(self, text_out: str | None) -> dict[str, Any]:
        """Parse JSON result from MCP browser_run_code response."""
        if not isinstance(text_out, str) or not text_out.strip():
            return {}
        import re
        # MCP wraps result in ### Result\n{json}\n### Ran Playwright code
        m = re.search(r"###\s*Result\s*\n([\s\S]*?)(?:\n###\s|$)", text_out, re.IGNORECASE)
        if m:
            chunk = m.group(1).strip()
            try:
                return json.loads(chunk)
            except Exception:
                pass
        # Try raw JSON parse.
        try:
            return json.loads(text_out.strip())
        except Exception:
            pass
        # Fallback: return raw text.
        return {"text": text_out.strip(), "url": "", "title": ""}


def get_tool_class():
    return WebGetContent
