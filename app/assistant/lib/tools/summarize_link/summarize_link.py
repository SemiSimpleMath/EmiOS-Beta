from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.scrape_url.utils import scrape
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult

logger = get_logger(__name__)


class SummarizeLinkTool(BaseTool):
    """
    Read-only webpage summarizer.
    """

    def __init__(self):
        super().__init__("summarize_link")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        arguments = tool_message.tool_data.get("arguments", {}) if tool_message.tool_data else {}
        url = str(arguments.get("url") or "").strip()
        focus = str(arguments.get("focus") or "").strip()

        if not url:
            return ToolResult(result_type="error", content="Missing required argument: url")

        if scrape.is_reddit_url(url):
            return ToolResult(
                result_type="error",
                content="Refused to summarize Reddit by policy.",
                data={"url": url, "policy": "no_reddit_scraping"},
            )

        try:
            result = scrape.scrape_page(url)
            page_text = result.get("full_text", "") if isinstance(result, dict) else ""
            if not page_text.strip():
                return ToolResult(
                    result_type="error",
                    content="Failed to read meaningful page text for summarization.",
                    data={"url": url},
                )

            summary_prompt = (
                "Summarize the webpage content in 5-8 concise bullets. "
                "Include key topic, who/what it is for, and notable facts. "
                "Do not invent anything."
            )
            if focus:
                summary_prompt += f" Focus specifically on: {focus}"

            scraper_agent = DI.agent_factory.create_agent("scraper")
            scraper_result = scraper_agent.action_handler(
                Message(
                    agent_input=(
                        f"What we are looking for: {summary_prompt}\n"
                        f"Raw content: {page_text}"
                    )
                )
            )

            summary_text = ""
            if hasattr(scraper_result, "data") and isinstance(scraper_result.data, dict):
                summary_text = str(scraper_result.data.get("content") or "").strip()
            if not summary_text:
                summary_text = str(getattr(scraper_result, "content", "") or "").strip()
            if not summary_text:
                summary_text = page_text[:1200]

            return ToolResult(
                result_type="link_summary",
                content=f"Summary of {url}:\n{summary_text}",
                data={
                    "url": url,
                    "summary": summary_text,
                    "focus": focus,
                },
            )
        except Exception as e:
            logger.error("summarize_link failed for url=%s: %s", url, e)
            logger.debug("summarize_link execution exception details", exc_info=True)
            return ToolResult(
                result_type="error",
                content=f"summarize_link failed: {e}",
                data={"url": url, "focus": focus},
            )


def get_tool_class():
    return SummarizeLinkTool
