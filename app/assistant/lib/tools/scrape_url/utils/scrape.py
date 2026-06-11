import re
import unicodedata
from typing import Optional
from urllib.parse import urlsplit, urljoin

import requests
import trafilatura
from bs4 import BeautifulSoup, Tag
from readability import Document

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

MINIMUM_CONTENT_LENGTH = 150

HEADING_TAGS = {"h1", "h2", "h3", "h4"}
TEXT_TAGS = {"p", "li", "blockquote", "pre"}

JUNK_SECTION_TITLES = {
    "related articles",
    "related content",
    "related posts",
    "footer",
    "follow us",
    "sign up",
    "newsletter",
    "advertisement",
    "share",
    "comments",
    "tags",
    "menu",
    "navigation",
    "home",
    "login",
    "sign in",
}


def is_reddit_url(url: Optional[str]) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return host == "redd.it" or host == "reddit.com" or host.endswith(".reddit.com")
    except Exception as e:
        logger.debug(f"is_reddit_url parse error: {e}")
        return False


def _is_wikipedia_article_url(url: Optional[str]) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return host.endswith("wikipedia.org") and (parts.path or "").startswith("/wiki/")
    except Exception as e:
        logger.debug(f"_is_wikipedia_article_url parse error: {e}")
        return False


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _tag_text(tag: Tag) -> str:
    return _normalize_text(tag.get_text(" ", strip=True))


def _is_meaningful_text(text: str, min_chars: int = 30) -> bool:
    if not text or len(text) < min_chars:
        return False
    return text.lower() not in {"menu", "home", "login", "sign up", "sign in"}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    try:
        if is_reddit_url(url):
            logger.warning(f"Refusing direct fetch for Reddit URL: {url}")
            return None

        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            logger.warning(f"Non-HTML content type for {url}: {content_type}")
            return None

        return response.text
    except requests.exceptions.RequestException as e:
        logger.debug(f"Direct request for {url} failed: {e}")
        return None


def fetch_rendered_html(url: str, timeout_ms: int = 20000) -> Optional[str]:
    if is_reddit_url(url):
        logger.warning(f"Refusing rendered fetch for Reddit URL: {url}")
        return None

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        logger.debug(f"Rendered fetch for {url} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_with_trafilatura(html_content: str, url: Optional[str] = None) -> str:
    try:
        extracted = trafilatura.extract(
            html_content,
            url=url,
            output_format="txt",
            include_links=False,
            include_images=False,
            favor_precision=True,
            deduplicate=True,
        )
        return _normalize_text(extracted or "")
    except Exception as e:
        logger.debug(f"Trafilatura extraction failed: {e}")
        return ""


def _extract_with_trafilatura_html(html_content: str, url: Optional[str] = None) -> str:
    """Returns cleaned HTML fragment from trafilatura for section splitting."""
    try:
        extracted = trafilatura.extract(
            html_content,
            url=url,
            output_format="html",
            include_links=False,
            include_images=False,
            favor_precision=True,
            deduplicate=True,
        )
        return extracted or ""
    except Exception as e:
        logger.debug(f"Trafilatura HTML extraction failed: {e}")
        return ""


def _extract_with_readability(html_content: str) -> tuple[str, str]:
    """Returns (summary_html, title) for further section splitting."""
    try:
        doc = Document(html_content)
        title = doc.title() or ""
        summary_html = doc.summary(html_partial=True)
        return summary_html, title
    except Exception as e:
        logger.debug(f"Readability extraction failed: {e}")
        return "", ""


def _extract_wikipedia(html_content: str) -> str:
    """Wikipedia-specific extraction: focuses on main article, drops navboxes and tail sections."""
    soup = BeautifulSoup(html_content or "", "html.parser")

    for element in soup(["script", "style", "noscript", "iframe"]):
        element.decompose()

    content = (
        soup.select_one("#mw-content-text .mw-parser-output")
        or soup.select_one("#mw-content-text")
    )
    if content is None:
        return ""

    for sel in [
        "#toc", ".mw-editsection", ".navbox", ".vertical-navbox", ".sidebar",
        ".infobox", ".ambox", ".mbox", ".hatnote", ".metadata", ".reflist",
        ".mw-references-wrap", "ol.references", "sup.reference",
        "table.navbox", "div.navbox",
    ]:
        for el in content.select(sel):
            el.decompose()

    stop_ids = {"References", "External_links", "Further_reading", "See_also", "Notes", "Bibliography"}
    for headline in content.select(".mw-headline[id]"):
        if headline.get("id") in stop_ids:
            heading = headline
            while heading is not None and heading.name not in {"h2", "h3", "h4"}:
                heading = heading.parent
            if heading is not None:
                sib = heading
                while sib is not None:
                    nxt = sib.find_next_sibling()
                    try:
                        sib.decompose()
                    except Exception as e:
                        logger.debug(f"Wikipedia sib decompose error: {e}")
                    sib = nxt
            break

    tags = content.find_all(["p", "h1", "h2", "h3", "h4", "li"])
    text_pieces = [t.get_text(separator=" ", strip=True) for t in tags]
    full_text = "\n".join(t for t in text_pieces if t)
    return _normalize_text(full_text)


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

def split_html_into_sections(
    cleaned_html: str,
    *,
    min_section_chars: int = 80,
    max_section_chars: int = 3000,
) -> list[dict]:
    """
    Split a cleaned HTML fragment into heading-based sections.

    Returns a list of dicts: {section_title, level, text, score}.
    """
    soup = BeautifulSoup(cleaned_html, "html.parser")

    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    body = soup.body or soup

    sections: list[dict] = []
    current_title = "Introduction"
    current_level = 1
    current_blocks: list[str] = []

    def flush_section() -> None:
        nonlocal current_blocks, current_title, current_level
        text = _normalize_text("\n\n".join(b for b in current_blocks if b))
        title_lower = current_title.lower().strip()
        if len(text) >= min_section_chars and title_lower not in JUNK_SECTION_TITLES:
            sections.append({
                "section_title": current_title,
                "level": current_level,
                "text": text,
                "score": 0.0,
            })
        current_blocks.clear()

    heading_seen = False

    for node in body.descendants:
        if not isinstance(node, Tag):
            continue

        name = node.name.lower() if node.name else ""

        if name in HEADING_TAGS:
            flush_section()
            heading_text = _tag_text(node)
            if heading_text:
                current_title = heading_text
                current_level = int(name[1])
                heading_seen = True
            continue

        if name in TEXT_TAGS:
            text = _tag_text(node)
            if _is_meaningful_text(text):
                current_blocks.append(text)

    flush_section()

    # If no heading was ever encountered the "Introduction" placeholder title
    # is meaningless — collapse everything into a single "Main Content" section.
    if not heading_seen and sections:
        combined = _normalize_text("\n\n".join(s["text"] for s in sections))
        sections.clear()
        if combined:
            sections.append({
                "section_title": "Main Content",
                "level": 1,
                "text": combined,
                "score": 0.0,
            })

    if not sections:
        text_blocks = [
            _tag_text(tag)
            for tag in body.find_all(["p", "li", "blockquote"])
            if _is_meaningful_text(_tag_text(tag))
        ]
        fallback_text = _normalize_text("\n\n".join(text_blocks))
        if fallback_text:
            sections.append({
                "section_title": "Main Content",
                "level": 1,
                "text": fallback_text,
                "score": 0.0,
            })

    final_sections: list[dict] = []
    for section in sections:
        text = section["text"]
        if len(text) <= max_section_chars:
            final_sections.append(section)
            continue

        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunk: list[str] = []
        chunk_len = 0
        chunk_idx = 1

        for para in paragraphs:
            para_len = len(para)
            if chunk and chunk_len + para_len + 2 > max_section_chars:
                suffix = f" ({chunk_idx})" if chunk_idx > 1 else ""
                final_sections.append({
                    "section_title": f'{section["section_title"]}{suffix}',
                    "level": section["level"],
                    "text": _normalize_text("\n\n".join(chunk)),
                    "score": 0.0,
                })
                chunk = [para]
                chunk_len = para_len
                chunk_idx += 1
            else:
                chunk.append(para)
                chunk_len += para_len + 2

        if chunk:
            suffix = f" ({chunk_idx})" if chunk_idx > 1 else ""
            final_sections.append({
                "section_title": f'{section["section_title"]}{suffix}',
                "level": section["level"],
                "text": _normalize_text("\n\n".join(chunk)),
                "score": 0.0,
            })

    return final_sections


def score_sections_against_query(sections: list[dict], query: str) -> list[dict]:
    """Score and sort sections by relevance to a query using simple term frequency."""
    terms = [t.lower() for t in re.findall(r"\b[a-zA-Z0-9]{3,}\b", query or "")]
    if not terms:
        return sections

    for section in sections:
        haystack = f'{section["section_title"]}\n{section["text"]}'.lower()
        score = 0.0
        for term in terms:
            count = haystack.count(term)
            if count:
                score += min(count, 5)
            if term in section["section_title"].lower():
                score += 3.0
        section["score"] = score

    return sorted(sections, key=lambda x: x["score"], reverse=True)


# ---------------------------------------------------------------------------
# Content quality assessment
# ---------------------------------------------------------------------------

# Extraction path constants — used as values in result["extraction_path"].
_PATH_WIKIPEDIA = "wikipedia"
_PATH_TRAFILATURA = "trafilatura"
_PATH_TRAFILATURA_PLAIN = "trafilatura_plain"
_PATH_READABILITY = "readability"
_PATH_BEAUTIFULSOUP = "beautifulsoup"

# Quality constants — used as values in result["content_quality"].
QUALITY_GOOD = "good"
QUALITY_NAV_ONLY = "nav_only"
QUALITY_THIN = "thin"

# Paths that reliably extract article body text.
_GOOD_PATHS = {_PATH_WIKIPEDIA, _PATH_TRAFILATURA, _PATH_TRAFILATURA_PLAIN, _PATH_READABILITY}

# nav_only detection thresholds for the BeautifulSoup fallback path.
# A portal/menu page has many tiny label-sized blocks; a real content page has long paragraphs.
_NAV_SHORT_BLOCK_CHARS = 80    # blocks shorter than this count as "short"
_NAV_BLOCK_RATIO = 0.85        # if >=85% of blocks are short...
_NAV_MEAN_BLOCK_CHARS = 60     # ...and mean block length is below this → nav_only


def _assess_content_quality(full_text: str, extraction_path: str) -> str:
    """
    Return one of QUALITY_GOOD / QUALITY_NAV_ONLY / QUALITY_THIN.

    Rules (in order):
    - If extraction succeeded via a reliable path, trust it and return "good"
      unless the total text is very short (< 500 chars), in which case "thin".
    - If we fell to BeautifulSoup last-resort, apply two heuristics:
      1. Average block length: real content pages have long paragraphs; portal/nav
         pages have many short label-sized blocks. If the mean block length is
         below NAV_MEAN_BLOCK_CHARS and more than NAV_BLOCK_RATIO of blocks are
         shorter than NAV_SHORT_BLOCK_CHARS, classify as "nav_only".
      2. If total text < 500 chars, classify as "thin".
      Otherwise return "good" (BS found real paragraph content).
    """
    if extraction_path in _GOOD_PATHS:
        return QUALITY_GOOD if len(full_text) >= 500 else QUALITY_THIN

    # BeautifulSoup path — use block-length statistics.
    blocks = [b.strip() for b in full_text.split("\n\n") if b.strip()]
    if not blocks:
        return QUALITY_THIN

    # Too short to have any meaningful content — classify as thin regardless of structure.
    if len(full_text) < 500:
        return QUALITY_THIN

    avg_len = sum(len(b) for b in blocks) / len(blocks)
    short_ratio = sum(1 for b in blocks if len(b) < _NAV_SHORT_BLOCK_CHARS) / len(blocks)

    if avg_len < _NAV_MEAN_BLOCK_CHARS and short_ratio >= _NAV_BLOCK_RATIO:
        return QUALITY_NAV_ONLY
    return QUALITY_GOOD


def _extract_nav_links(html: str, base_url: str, max_links: int = 30) -> list[dict]:
    """
    Extract unique internal navigation links from a page.

    Returns a list of {"text": str, "url": str} dicts, capped at max_links,
    sorted by URL length (shorter = higher-level page first).
    Only follows same-host links so the agent stays on the same site.
    Skips anchors, javascript: hrefs, and links with no visible label.
    """
    try:
        base_host = (urlsplit(base_url).hostname or "").lower()
        soup = BeautifulSoup(html, "html.parser")

        seen_urls: set[str] = set()
        links: list[dict] = []

        for a in soup.find_all("a", href=True):
            href = (a["href"] or "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue

            full_url = urljoin(base_url, href)
            parsed = urlsplit(full_url)
            link_host = (parsed.hostname or "").lower()

            if link_host != base_host:
                continue
            if full_url in seen_urls:
                continue

            text = a.get_text(strip=True)
            if not text or len(text) > 60:
                continue

            seen_urls.add(full_url)
            links.append({"text": text, "url": full_url})

        links.sort(key=lambda x: len(x["url"]))
        return links[:max_links]
    except Exception as e:
        logger.debug(f"_extract_nav_links failed for {base_url}: {e}")
        return []


# ---------------------------------------------------------------------------
# Page scraping
# ---------------------------------------------------------------------------

def scrape_page(url: str) -> Optional[dict]:
    """
    Scrape a URL and return a structured result dict:
        {
            "url": str,
            "title": str,
            "sections": list[dict],      # [{section_title, level, text, score}]
            "full_text": str,
            "extraction_path": str,      # which extractor succeeded
            "content_quality": str,      # "good" | "nav_only" | "thin"
        }

    content_quality tells the caller whether the content is trustworthy:
      "good"     — reliable article body was extracted; proceed normally.
      "nav_only" — page has no article body; only navigation menus were found.
                   Do not retry this URL; try a different one.
      "thin"     — some content was found but it is very short; may be
                   a landing page, login wall, or JS-gated content.

    Fast path: requests -> trafilatura.
    Slow path: Playwright render -> trafilatura.
    Falls back through readability-lxml then plain BeautifulSoup.
    Returns None only if all fetch+extraction paths fail entirely.
    """
    if is_reddit_url(url):
        logger.warning(f"Refusing scrape_page for Reddit URL: {url}")
        return None

    title = ""

    def _result(
        sections: list[dict],
        full_text: str,
        path: str,
        *,
        extra_title: str = "",
        nav_links: list[dict] | None = None,
    ) -> dict:
        quality = _assess_content_quality(full_text, path)
        logger.info(
            f"scrape_page [{path}] quality={quality} "
            f"sections={len(sections)} chars={len(full_text)} url={url}"
        )
        return {
            "url": url,
            "title": extra_title or title,
            "sections": sections,
            "full_text": full_text,
            "extraction_path": path,
            "content_quality": quality,
            "nav_links": nav_links or [],
        }

    for attempt, fetch_fn in [
        ("direct", lambda: fetch_html(url)),
        ("rendered", lambda: fetch_rendered_html(url)),
    ]:
        logger.info(f"Scrape attempt [{attempt}] for {url}")
        html = fetch_fn()
        if not html:
            logger.warning(f"No HTML from [{attempt}] for {url}")
            continue

        # Wikipedia gets its own dedicated extractor; return immediately.
        if _is_wikipedia_article_url(url):
            text = _extract_wikipedia(html)
            if len(text) >= MINIMUM_CONTENT_LENGTH:
                return _result(
                    [{"section_title": "Article", "level": 1, "text": text, "score": 0.0}],
                    text,
                    _PATH_WIKIPEDIA,
                )

        # --- Try trafilatura HTML output first (keeps structure for splitting) ---
        cleaned_html = _extract_with_trafilatura_html(html, url=url)
        if cleaned_html:
            sections = split_html_into_sections(cleaned_html)
            full_text = _normalize_text("\n\n".join(s["text"] for s in sections))
            if len(full_text) >= MINIMUM_CONTENT_LENGTH:
                return _result(sections, full_text, _PATH_TRAFILATURA)

        # --- trafilatura plain text fallback ---
        plain_text = _extract_with_trafilatura(html, url=url)
        if len(plain_text) >= MINIMUM_CONTENT_LENGTH:
            return _result(
                [{"section_title": "Main Content", "level": 1, "text": plain_text, "score": 0.0}],
                plain_text,
                _PATH_TRAFILATURA_PLAIN,
            )

        # --- readability-lxml fallback ---
        summary_html, doc_title = _extract_with_readability(html)
        if not title and doc_title:
            title = doc_title
        if summary_html:
            sections = split_html_into_sections(summary_html)
            full_text = _normalize_text("\n\n".join(s["text"] for s in sections))
            if len(full_text) >= MINIMUM_CONTENT_LENGTH:
                return _result(sections, full_text, _PATH_READABILITY, extra_title=doc_title)

        # --- last-resort conservative BeautifulSoup pass ---
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
                tag.decompose()
            blocks = [
                tag.get_text(" ", strip=True)
                for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "article", "section"])
            ]
            bs_text = _normalize_text("\n\n".join(b for b in blocks if b))
            if len(bs_text) >= MINIMUM_CONTENT_LENGTH:
                nav_links = _extract_nav_links(html, url)
                return _result(
                    [{"section_title": "Main Content", "level": 1, "text": bs_text, "score": 0.0}],
                    bs_text,
                    _PATH_BEAUTIFULSOUP,
                    nav_links=nav_links,
                )
        except Exception as e:
            logger.debug(f"BeautifulSoup last-resort [{attempt}] failed: {e}")

    logger.error(f"All extraction paths failed for {url}")
    return None


if __name__ == "__main__":
    test_urls = [
        "https://www.daveandbusters.com/us/en/about/locations/rancho-mirage",
        "https://meadowpark.iusd.org/",
    ]

    for test_url in test_urls:
        print(f"\n{'='*60}")
        print(f"URL: {test_url}")
        result = scrape_page(test_url)
        if result:
            print(f"Title: {result['title']}")
            print(f"Sections: {len(result['sections'])}")
            print(f"Full text length: {len(result['full_text'])}")
            for i, sec in enumerate(result["sections"][:3]):
                print(f"\n  [{i+1}] {sec['section_title']} (level {sec['level']})")
                print(f"      {sec['text'][:200]}...")
        else:
            print("FAILED: No content extracted.")
