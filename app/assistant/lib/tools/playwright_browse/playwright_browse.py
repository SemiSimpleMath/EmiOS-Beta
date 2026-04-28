from __future__ import annotations

from typing import Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, Message, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PlaywrightBrowse(BaseTool):
    """
    Render and extract content from JS-heavy pages using Playwright.

    Notes:
    - Supports a `dry_run` mode so managers/tests can validate orchestration without launching a browser.
    - Does NOT handle logins, payments, or sensitive actions by default (keep this tool read-only).
    """

    def __init__(self):
        super().__init__("playwright_browse")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}

        url = (arguments.get("url") or "").strip()
        query = (arguments.get("query") or "").strip()
        dry_run = bool(arguments.get("dry_run", False))

        wait_until = (arguments.get("wait_until") or "domcontentloaded").strip()
        timeout_ms = int(arguments.get("timeout_ms") or 30_000)
        post_wait_ms = int(arguments.get("post_wait_ms") or 0)
        max_chars = int(arguments.get("max_chars") or 50_000)

        if not url:
            return ToolResult(
                result_type="error",
                content="Error: 'url' is required for playwright_browse.",
            )

        if dry_run:
            return ToolResult(
                result_type="playwright_browse",
                content=(
                    "DRY RUN (no browser launched)\n"
                    f"url: {url}\n"
                    f"wait_until: {wait_until}\n"
                    f"timeout_ms: {timeout_ms}\n"
                    f"post_wait_ms: {post_wait_ms}\n"
                    f"query: {query or '[none]'}"
                ),
                data={"dry_run": True, "url": url},
            )

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception as e:
            return ToolResult(
                result_type="error",
                content=(
                    "playwright_browse requires Playwright.\n"
                    "Install:\n"
                    "  pip install playwright\n"
                    "  playwright install\n"
                    f"Import error: {e}"
                ),
            )

        # Render the page
        html: Optional[str] = None
        visible_text: Optional[str] = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                if post_wait_ms > 0:
                    page.wait_for_timeout(post_wait_ms)
                html = page.content()
                try:
                    visible_text = page.inner_text("body")
                except Exception:
                    visible_text = None
                context.close()
                browser.close()
        except Exception as e:
            logger.error(f"playwright_browse failed for {url}: {e}", exc_info=True)
            return ToolResult(
                result_type="error",
                content=f"Error in playwright_browse: {e}",
                data={"url": url},
            )

        text = (visible_text or "").strip()
        if not text and html:
            # Fallback: keep HTML if the body text extraction fails (some pages block it).
            text = html

        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n\n[truncated]"

        # Optional query extraction via existing "scraper" agent (keeps behavior consistent with scrape_url).
        if query:
            try:
                scraper_agent = DI.agent_factory.create_agent("scraper")
                scraper_result = scraper_agent.action_handler(
                    Message(
                        agent_input=(
                            f"What we are looking for: {query}\n"
                            f"Raw content: {text}"
                        )
                    )
                )
                if hasattr(scraper_result, "data") and scraper_result.data:
                    extracted = scraper_result.data.get("content", "") or ""
                else:
                    extracted = getattr(scraper_result, "content", "") or ""

                if max_chars and len(extracted) > max_chars:
                    extracted = extracted[:max_chars] + "\n\n[truncated]"

                text_out = (
                    f"Playwright browsed URL: {url}\n"
                    f"Query: {query}\n"
                    f"Relevant content:\n{extracted}"
                )
                return ToolResult(
                    result_type="playwright_browse",
                    content=text_out,
                    data={"url": url, "query": query},
                )
            except Exception as e:
                logger.warning(f"playwright_browse: scraper agent failed, returning raw text: {e}")

        return ToolResult(
            result_type="playwright_browse",
            content=f"Playwright browsed URL: {url}\n\n{text}",
            data={"url": url, "query": query},
        )


def get_tool_class():
    return PlaywrightBrowse

