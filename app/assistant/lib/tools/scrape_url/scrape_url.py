from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.scrape_url.utils import scrape
from app.assistant.lib.tools.scrape_url.utils.scrape import (
    QUALITY_GOOD,
    QUALITY_NAV_ONLY,
    QUALITY_THIN,
)
from app.assistant.utils.pydantic_classes import (
    ToolMessage,
    Message,
    ToolResult,
)

from app.assistant.utils.logging_config import get_logger
from urllib.parse import urlsplit

logger = get_logger(__name__)

_NAV_ONLY_BASE = (
    "NOTE: This page appears to be a navigation/portal page with no article body. "
    "All extractors found only menus and link labels, not content paragraphs. "
    "Do not retry this URL. Try one of the sub-pages listed below instead."
)
_THIN_WARNING = (
    "NOTE: Only a small amount of content was extracted from this page. "
    "It may be behind a login wall, require JavaScript rendering, or simply have very little text. "
    "Consider trying a different URL or a more specific page."
)


def _build_quality_warning(quality: str, nav_links: list[dict]) -> str:
    if quality == QUALITY_NAV_ONLY:
        if nav_links:
            links_text = "\n".join(
                f"  - {lnk['text']}: {lnk['url']}" for lnk in nav_links
            )
            return f"{_NAV_ONLY_BASE}\n\nAvailable sub-pages:\n{links_text}"
        return _NAV_ONLY_BASE
    if quality == QUALITY_THIN:
        return _THIN_WARNING
    return ""


def _is_reddit_url(url: str) -> bool:
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        return host in {"redd.it"} or host == "reddit.com" or host.endswith(".reddit.com")
    except Exception as e:
        logger.debug(f"_is_reddit_url parse error: {e}")
        return False


class ScrapeURL(BaseTool):
    """
    Tool to scrape web content based on a query and URL.
    Returns structured sections and full text rather than a raw flattened blob.
    """

    def __init__(self):
        # result_type "scrape" is handled in DataConversionModule._convert_scrape.
        super().__init__("scrape")
        self.agent_factory = None

    def execute(self, tool_message: "ToolMessage") -> ToolResult:
        arguments = tool_message.tool_data.get("arguments", {})
        query = arguments.get("query", "") or ""
        url = arguments.get("url")

        if not url:
            logger.error("'url' argument is required for scrape tool.")
            return ToolResult(
                result_type="error",
                content="Error: 'url' argument is required for scrape tool.",
            )

        if _is_reddit_url(url):
            return ToolResult(
                result_type="error",
                content=(
                    "Refused to scrape Reddit by policy. "
                    "Reddit pages are off-limits for automated scraping in Emi."
                ),
                data={"url": url, "policy": "no_reddit_scraping"},
            )

        try:
            page = scrape.scrape_page(url)
            if not page:
                raise RuntimeError("Failed to scrape any meaningful content from the page.")

            sections = page["sections"]
            full_text = page["full_text"]
            title = page.get("title", "")
            quality = page.get("content_quality", QUALITY_GOOD)
            extraction_path = page.get("extraction_path", "unknown")
            nav_links = page.get("nav_links", [])
            quality_warning = _build_quality_warning(quality, nav_links)

            if query:
                ranked = scrape.score_sections_against_query(sections, query)
                top_sections = ranked[:5]

                scraper_agent = DI.agent_factory.create_agent("scraper")
                scraper_result = scraper_agent.action_handler(
                    Message(
                        agent_input=_build_agent_input(
                            url, title, query, top_sections, full_text, quality_warning
                        )
                    )
                )

                logger.info(f"Scraper agent result type: {type(scraper_result)}")

                if hasattr(scraper_result, "data") and scraper_result.data:
                    extracted_text = scraper_result.data.get("content", "") or ""
                    raw_links = scraper_result.data.get("links", []) or []
                else:
                    extracted_text = scraper_result.content or ""
                    raw_links = scraper_result.data_list or []
                    logger.warning("Scraper result has no .data field, using fallback")

                normalized_links = _normalize_links(raw_links)

                content_parts = [f"Scraped URL: {url}", f"Query: {query}"]
                if quality_warning:
                    content_parts.append(quality_warning)
                content_parts.append(f"Relevant content:\n{extracted_text}")
                content = "\n".join(content_parts)

                return ToolResult(
                    result_type="scrape",
                    content=content,
                    data_list=normalized_links,
                    data={
                        "url": url,
                        "title": title,
                        "query": query,
                        "sections": sections,
                        "content_quality": quality,
                        "extraction_path": extraction_path,
                        "nav_links": nav_links,
                    },
                )

            else:
                content_parts = [f"Scraped URL: {url}"]
                if quality_warning:
                    content_parts.append(quality_warning)
                content_parts.append(f"Full page content:\n{full_text}")
                content = "\n".join(content_parts)

                return ToolResult(
                    result_type="scrape",
                    content=content,
                    data_list=[],
                    data={
                        "url": url,
                        "title": title,
                        "sections": sections,
                        "content_quality": quality,
                        "extraction_path": extraction_path,
                        "nav_links": nav_links,
                    },
                )

        except Exception as e:
            logger.error(f"Error in execute scrape: {e}")
            return ToolResult(
                result_type="error",
                data_type="error",
                content=f"Error in execute scrape: {e}",
            )


def _build_agent_input(
    url: str,
    title: str,
    query: str,
    top_sections: list[dict],
    full_text: str,
    quality_warning: str = "",
) -> str:
    sections_text = "\n\n".join(
        f"[{i + 1}] {s['section_title']}\n{s['text']}"
        for i, s in enumerate(top_sections)
    )
    parts = [
        f"What we are looking for: {query}",
        f"URL: {url}",
        f"Page title: {title}",
    ]
    if quality_warning:
        parts.append(quality_warning)
    parts += [
        f"\nMost relevant sections (ranked by keyword match):\n{sections_text}",
        f"\nFull text fallback (first 12000 chars):\n{full_text[:12000]}",
    ]
    return "\n".join(parts)


def _normalize_links(raw_links: list) -> list[dict]:
    result = []
    for item in raw_links:
        if isinstance(item, dict):
            url_val = item.get("url")
            desc_val = item.get("description", "")
        else:
            url_val = getattr(item, "url", None)
            desc_val = getattr(item, "description", "") or ""

        if not url_val:
            continue
        result.append({"url": url_val, "description": desc_val})
    return result


def get_tool_class():
    """Required by the tool registry."""
    return ScrapeURL


# === TEST BLOCK ===
if __name__ == "__main__":
    import app.assistant.tests.test_setup  # noqa: F401 - registers DI
    from app.assistant.utils.pydantic_classes import ToolMessage

    mock_with_query = ToolMessage(
        data_type="tool_request",
        sender="test_user",
        receiver="ScrapeTool",
        task="scrape url",
        tool_name="scrape_url",
        tool_data={
            "tool_name": "scrape_url",
            "arguments": {
                "query": (
                    "Find today's (Sunday) hours and house policies for minors "
                    "(under 21), cover charges, and confirm address / being inside The River. "
                    "Look for 'House Policies', 'Hours', 'Under 21', 'Guardian', "
                    "'Curfew', 'Cover Charge'."
                ),
                "url": "https://www.daveandbusters.com/us/en/about/locations/rancho-mirage",
            },
        },
    )

    mock_full_page = ToolMessage(
        data_type="tool_request",
        sender="test_user",
        receiver="ScrapeTool",
        task="scrape url",
        tool_name="scrape_url",
        tool_data={
            "tool_name": "scrape_url",
            "arguments": {
                "url": "https://www.daveandbusters.com/us/en/about/locations/rancho-mirage",
                "query": "",
            },
        },
    )

    scrape_url = ScrapeURL()

    print("=== Testing with query ===")
    result_with_query = scrape_url.execute(mock_with_query)
    print(result_with_query.content)

    print("\n=== Testing without query (full page) ===")
    result_full_page = scrape_url.execute(mock_full_page)
    print(result_full_page.content)
    if result_full_page.data:
        print(f"Sections: {len(result_full_page.data.get('sections', []))}")
