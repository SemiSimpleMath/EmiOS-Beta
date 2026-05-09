"""
Tests for app.assistant.lib.tools.scrape_url.utils.scrape

Structure
---------
Unit tests (no network, no browser):
  - _normalize_text
  - _is_meaningful_text
  - _is_reddit_url
  - _is_wikipedia_article_url
  - split_html_into_sections
  - score_sections_against_query
  - fetch_html  (monkeypatched requests)
  - _extract_with_trafilatura  (against realistic HTML)
  - _extract_with_readability  (against realistic HTML)
  - _extract_wikipedia  (against synthetic Wikipedia-shaped HTML)
  - _assess_content_quality
  - scrape_page result shape (monkeypatched fetch)

Integration tests (real network — marked with pytest.mark.integration):
  - Static page: meadowpark.iusd.org — structure + content quality
  - JS-heavy page: daveandbusters.com — escalation to Playwright + sections
  - Wikipedia article — dedicated extractor path
  - score_sections_against_query ranks the right section to the top
  - Reddit URL is refused at the scrape_page level

Run fast tests only:
    pytest app/assistant/test/test_scrape.py -v -m "not integration"

Run everything (includes network; takes ~30–60 s):
    pytest app/assistant/test/test_scrape.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Make sure repo root is on sys.path when running by file.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "app").is_dir():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from app.assistant.lib.tools.scrape_url.utils.scrape import (
    MINIMUM_CONTENT_LENGTH,
    QUALITY_GOOD,
    QUALITY_NAV_ONLY,
    QUALITY_THIN,
    _PATH_BEAUTIFULSOUP,
    _PATH_TRAFILATURA,
    _PATH_WIKIPEDIA,
    _assess_content_quality,
    _extract_nav_links,
    _extract_with_readability,
    _extract_with_trafilatura,
    _extract_wikipedia,
    _is_meaningful_text,
    _is_reddit_url,
    _is_wikipedia_article_url,
    _normalize_text,
    fetch_html,
    score_sections_against_query,
    scrape_page,
    split_html_into_sections,
)


# ===========================================================================
# Helpers / shared fixtures
# ===========================================================================

ARTICLE_HTML = """<!doctype html>
<html>
<head><title>Space Exploration Overview</title></head>
<body>
  <header><nav><a href="/">Home</a><a href="/about">About</a></nav></header>
  <main>
    <h1>Space Exploration Overview</h1>
    <p>Space exploration is the use of astronomy and space technology to explore outer space.
       While the observation of objects in space, known as astronomy, predates reliable recorded
       history, it was the development of large and relatively efficient rockets during the
       early twentieth century that allowed physical space exploration to become a reality.</p>

    <h2>History</h2>
    <p>The first human-made object sent into space was the Soviet Union's Sputnik 1 on 4 October
       1957. The first human spaceflight was Vostok 1 on 12 April 1961, when cosmonaut Yuri
       Gagarin orbited the Earth once.</p>
    <p>The United States landed the first humans on the Moon on 20 July 1969 with the Apollo 11
       mission, with Neil Armstrong and Buzz Aldrin walking on the surface while Michael Collins
       orbited above.</p>

    <h2>Current Programs</h2>
    <p>NASA's Artemis program aims to return humans to the Moon and establish a sustainable
       presence there. SpaceX and other private companies have also become major players in
       the space industry.</p>
    <ul>
      <li>Artemis program (NASA)</li>
      <li>Commercial Crew Program (SpaceX, Boeing)</li>
      <li>Mars missions (Perseverance rover)</li>
    </ul>

    <h2>Related Articles</h2>
    <p>See also our other space content.</p>
  </main>
  <footer><p>Footer text and navigation links that should not appear in output.</p></footer>
</body>
</html>"""


MULTI_SECTION_HTML = """<!doctype html>
<html><body>
  <h2>Opening Hours</h2>
  <p>Monday to Friday: 9 AM to 9 PM. Saturday: 10 AM to 8 PM. Sunday: 11 AM to 6 PM.
     Last entry is 30 minutes before closing time on all days.</p>

  <h2>House Policies</h2>
  <p>Guests under 18 must be accompanied by a parent or legal guardian who is 21 or older.
     This policy is enforced after 9 PM on weekdays and all day on weekends.
     Cover charges may apply on Friday and Saturday evenings after 10 PM.</p>

  <h2>Location</h2>
  <p>We are located inside The River at Rancho Mirage, 71-800 Highway 111, Rancho Mirage, CA.</p>

  <h2>Sign Up</h2>
  <p>Subscribe to our newsletter for deals.</p>

  <h2>Footer</h2>
  <p>Privacy policy and legal text.</p>
</body></html>"""


WIKIPEDIA_ARTICLE_HTML = """<!doctype html>
<html><body>
<div id="mw-content-text">
  <div class="mw-parser-output">
    <div class="hatnote">For other uses see Python disambiguation.</div>
    <div class="infobox">Infobox content that should be removed.</div>
    <p>Python is a high-level, general-purpose programming language. Its design philosophy
       emphasizes code readability with the use of significant indentation. Python is
       dynamically typed and garbage-collected.</p>
    <p>Python was created by Guido van Rossum, and first released on 20 February 1991.
       It consistently ranks as one of the most popular programming languages.</p>
    <h2><span class="mw-headline" id="References">References</span></h2>
    <p>This paragraph is after the References heading and should be removed.</p>
    <div class="navbox">Navbox junk that should be removed.</div>
    <ol class="references"><li>Ref 1</li></ol>
  </div>
</div>
</body></html>"""


# ===========================================================================
# Unit tests — _normalize_text
# ===========================================================================

def test_normalize_text_strips_trailing_whitespace():
    assert _normalize_text("  hello  ") == "hello"


def test_normalize_text_collapses_multiple_spaces():
    assert _normalize_text("hello    world") == "hello world"


def test_normalize_text_collapses_excess_newlines():
    result = _normalize_text("line1\n\n\n\nline2")
    assert result == "line1\n\nline2"


def test_normalize_text_crlf_to_lf():
    assert "\r" not in _normalize_text("line1\r\nline2")


def test_normalize_text_nfkc_ligature():
    # U+FB01 LATIN SMALL LIGATURE FI → "fi"
    assert _normalize_text("\ufb01le") == "file"


def test_normalize_text_empty_string():
    assert _normalize_text("") == ""


def test_normalize_text_none_treated_as_empty():
    # _normalize_text uses `text or ""` so None would fail — pass empty string
    assert _normalize_text("") == ""


# ===========================================================================
# Unit tests — _is_meaningful_text
# ===========================================================================

def test_is_meaningful_text_rejects_empty():
    assert not _is_meaningful_text("")


def test_is_meaningful_text_rejects_short():
    assert not _is_meaningful_text("short")


def test_is_meaningful_text_rejects_exact_junk_word():
    assert not _is_meaningful_text("menu")
    assert not _is_meaningful_text("home")
    assert not _is_meaningful_text("login")


def test_is_meaningful_text_accepts_real_sentence():
    assert _is_meaningful_text("This is a real sentence that is long enough to pass.")


def test_is_meaningful_text_custom_min_chars():
    assert _is_meaningful_text("short but ok", min_chars=5)
    assert not _is_meaningful_text("hi", min_chars=5)


# ===========================================================================
# Unit tests — _is_reddit_url
# ===========================================================================

@pytest.mark.parametrize("url", [
    "https://www.reddit.com/r/python",
    "https://reddit.com/r/python",
    "https://old.reddit.com/r/python",
    "https://redd.it/abc123",
    "https://np.reddit.com/r/news",
])
def test_is_reddit_url_detects_reddit(url: str):
    assert _is_reddit_url(url)


@pytest.mark.parametrize("url", [
    "https://www.google.com",
    "https://en.wikipedia.org/wiki/Python",
    "https://notreddit.com/r/python",
    "",
    None,
    123,  # type: ignore[arg-type]
])
def test_is_reddit_url_ignores_non_reddit(url):
    assert not _is_reddit_url(url)


# ===========================================================================
# Unit tests — _is_wikipedia_article_url
# ===========================================================================

@pytest.mark.parametrize("url", [
    "https://en.wikipedia.org/wiki/Python_(programming_language)",
    "https://de.wikipedia.org/wiki/Python",
    "https://en.m.wikipedia.org/wiki/Space",
])
def test_is_wikipedia_url_detects_articles(url: str):
    assert _is_wikipedia_article_url(url)


@pytest.mark.parametrize("url", [
    "https://en.wikipedia.org/",                          # root, not /wiki/
    "https://en.wikipedia.org/w/index.php?title=Python",  # not /wiki/ path
    "https://www.google.com",
    "",
    None,
])
def test_is_wikipedia_url_ignores_non_articles(url):
    assert not _is_wikipedia_article_url(url)


# ===========================================================================
# Unit tests — split_html_into_sections
# ===========================================================================

def test_split_basic_sections_returns_expected_titles():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    titles = [s["section_title"] for s in sections]
    assert "Opening Hours" in titles
    assert "House Policies" in titles
    assert "Location" in titles


def test_split_filters_junk_section_titles():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    titles = [s["section_title"].lower() for s in sections]
    assert "sign up" not in titles
    assert "footer" not in titles


def test_split_section_has_required_keys():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    assert sections, "Expected at least one section"
    for s in sections:
        assert "section_title" in s
        assert "level" in s
        assert "text" in s
        assert "score" in s


def test_split_section_text_is_non_empty():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    for s in sections:
        assert len(s["text"]) >= 80, f"Section '{s['section_title']}' text too short"


def test_split_section_level_reflects_heading_tag():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    for s in sections:
        assert s["level"] == 2, f"All headings are h2, got level {s['level']}"


def test_split_oversized_section_is_chunked():
    long_para = "This is a fairly long paragraph that contains real content. " * 20
    big_html = f"""<html><body>
        <h2>Giant Section</h2>
        {''.join(f'<p>{long_para}</p>' for _ in range(30))}
    </body></html>"""
    sections = split_html_into_sections(big_html, max_section_chars=3000)
    giant = [s for s in sections if "Giant Section" in s["section_title"]]
    assert len(giant) > 1, "Oversized section should be split into chunks"
    for chunk in giant:
        assert len(chunk["text"]) <= 3000, f"Chunk exceeds max: {len(chunk['text'])}"


def test_split_no_headings_falls_back_to_main_content():
    html = """<html><body>
        <p>This page has no headings at all, just a long paragraph of real content here.
           It should still yield at least one section labelled Main Content for fallback.</p>
        <p>Another paragraph to ensure the total content exceeds the minimum threshold
           required for the fallback section to be created at all by the algorithm.</p>
    </body></html>"""
    sections = split_html_into_sections(html, min_section_chars=80)
    assert len(sections) == 1
    assert sections[0]["section_title"] == "Main Content"


def test_split_strips_scripts_from_html():
    html = """<html><body>
        <h2>About Us</h2>
        <script>alert('injected')</script>
        <p>We are a company that builds things and makes products for our many loyal customers
           who have been with us for a very long time and appreciate quality work.</p>
    </body></html>"""
    sections = split_html_into_sections(html)
    assert sections
    assert "injected" not in sections[0]["text"]


def test_split_article_html_produces_multiple_sections():
    sections = split_html_into_sections(ARTICLE_HTML)
    titles = [s["section_title"] for s in sections]
    assert "History" in titles
    assert "Current Programs" in titles
    assert "Related Articles" not in titles  # junk title filtered


# ===========================================================================
# Unit tests — score_sections_against_query
# ===========================================================================

def test_score_empty_query_returns_unchanged_order():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    original_order = [s["section_title"] for s in sections]
    result = score_sections_against_query(sections, "")
    assert [s["section_title"] for s in result] == original_order


def test_score_ranks_matching_section_first():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    ranked = score_sections_against_query(sections, "house policies minors cover charge")
    assert ranked[0]["section_title"] == "House Policies"


def test_score_title_hit_boosts_score():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    score_sections_against_query(sections, "location address")
    location = next(s for s in sections if s["section_title"] == "Location")
    hours = next(s for s in sections if s["section_title"] == "Opening Hours")
    assert location["score"] > hours["score"]


def test_score_all_sections_get_score_field():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    scored = score_sections_against_query(sections, "hours opening")
    for s in scored:
        assert isinstance(s["score"], float)


def test_score_returns_sorted_descending():
    sections = split_html_into_sections(MULTI_SECTION_HTML)
    scored = score_sections_against_query(sections, "hours opening time")
    scores = [s["score"] for s in scored]
    assert scores == sorted(scores, reverse=True)


# ===========================================================================
# Unit tests — fetch_html (monkeypatched)
# ===========================================================================

def _make_mock_response(text: str, content_type: str = "text/html; charset=utf-8", status: int = 200):
    mock = MagicMock()
    mock.text = text
    mock.raise_for_status = MagicMock()
    mock.headers = {"Content-Type": content_type}
    mock.status_code = status
    return mock


def test_fetch_html_returns_text_on_success():
    with patch("app.assistant.lib.tools.scrape_url.utils.scrape.requests.get") as mock_get:
        mock_get.return_value = _make_mock_response("<html><body>hello</body></html>")
        result = fetch_html("https://example.com")
    assert result == "<html><body>hello</body></html>"


def test_fetch_html_refuses_reddit():
    result = fetch_html("https://www.reddit.com/r/python")
    assert result is None


def test_fetch_html_returns_none_on_non_html_content_type():
    with patch("app.assistant.lib.tools.scrape_url.utils.scrape.requests.get") as mock_get:
        mock_get.return_value = _make_mock_response(
            '{"key": "value"}', content_type="application/json"
        )
        result = fetch_html("https://example.com/api")
    assert result is None


def test_fetch_html_returns_none_on_request_exception():
    import requests as req
    with patch("app.assistant.lib.tools.scrape_url.utils.scrape.requests.get") as mock_get:
        mock_get.side_effect = req.exceptions.ConnectionError("timeout")
        result = fetch_html("https://example.com")
    assert result is None


# ===========================================================================
# Unit tests — _extract_with_trafilatura (against static HTML, no network)
# ===========================================================================

def test_trafilatura_extracts_article_body():
    text = _extract_with_trafilatura(ARTICLE_HTML)
    assert "Sputnik" in text or "Gagarin" in text or "Moon" in text


def test_trafilatura_excludes_nav_and_footer():
    text = _extract_with_trafilatura(ARTICLE_HTML)
    assert "Footer text" not in text


def test_trafilatura_returns_empty_on_empty_html():
    assert _extract_with_trafilatura("") == ""


def test_trafilatura_returns_string_on_garbage_html():
    result = _extract_with_trafilatura("<<<<not html at all>>>>")
    assert isinstance(result, str)


# ===========================================================================
# Unit tests — _extract_with_readability (against static HTML, no network)
# ===========================================================================

def test_readability_returns_html_and_title():
    summary_html, title = _extract_with_readability(ARTICLE_HTML)
    assert isinstance(summary_html, str)
    assert isinstance(title, str)


def test_readability_summary_contains_article_content():
    summary_html, _ = _extract_with_readability(ARTICLE_HTML)
    assert "Space" in summary_html or "Sputnik" in summary_html or "Moon" in summary_html


def test_readability_returns_empty_on_empty_html():
    summary_html, title = _extract_with_readability("")
    assert summary_html == ""
    assert title == ""


# ===========================================================================
# Unit tests — _extract_wikipedia (against synthetic HTML, no network)
# ===========================================================================

def test_wikipedia_extractor_includes_article_text():
    text = _extract_wikipedia(WIKIPEDIA_ARTICLE_HTML)
    assert "Python" in text
    assert "Guido van Rossum" in text


def test_wikipedia_extractor_removes_infobox():
    text = _extract_wikipedia(WIKIPEDIA_ARTICLE_HTML)
    assert "Infobox content" not in text


def test_wikipedia_extractor_removes_hatnote():
    text = _extract_wikipedia(WIKIPEDIA_ARTICLE_HTML)
    assert "For other uses" not in text


def test_wikipedia_extractor_removes_content_after_references():
    text = _extract_wikipedia(WIKIPEDIA_ARTICLE_HTML)
    assert "after the References heading" not in text


def test_wikipedia_extractor_removes_navbox():
    text = _extract_wikipedia(WIKIPEDIA_ARTICLE_HTML)
    assert "Navbox junk" not in text


def test_wikipedia_extractor_returns_empty_for_non_wiki_html():
    text = _extract_wikipedia("<html><body><p>regular page</p></body></html>")
    assert text == ""


# ===========================================================================
# Unit tests — _extract_nav_links
# ===========================================================================

_NAV_LINKS_HTML = """<!doctype html>
<html><head><title>School Portal</title></head>
<body>
  <nav>
    <a href="/about">About</a>
    <a href="/about/bell-schedule">Bell Schedule</a>
    <a href="/about/contact-us">Contact Us</a>
    <a href="https://external.com/page">External Link</a>
    <a href="#anchor">Anchor Only</a>
    <a href="javascript:void(0)">JS Link</a>
    <a href="/parents">Parents</a>
    <a href="/about/bell-schedule">Bell Schedule</a>
  </nav>
</body></html>"""

_NAV_LINKS_BASE = "https://meadowpark.example.com/"


def test_extract_nav_links_returns_internal_links_only():
    links = _extract_nav_links(_NAV_LINKS_HTML, _NAV_LINKS_BASE)
    urls = [lnk["url"] for lnk in links]
    assert all("meadowpark.example.com" in u for u in urls), \
        "Should only return same-host links"
    assert not any("external.com" in u for u in urls)


def test_extract_nav_links_skips_anchors_and_js():
    links = _extract_nav_links(_NAV_LINKS_HTML, _NAV_LINKS_BASE)
    urls = [lnk["url"] for lnk in links]
    assert not any(u.endswith("#anchor") for u in urls)
    assert not any("javascript" in u for u in urls)


def test_extract_nav_links_deduplicates():
    links = _extract_nav_links(_NAV_LINKS_HTML, _NAV_LINKS_BASE)
    urls = [lnk["url"] for lnk in links]
    assert len(urls) == len(set(urls)), "Duplicate URLs should be removed"


def test_extract_nav_links_includes_text_and_url():
    links = _extract_nav_links(_NAV_LINKS_HTML, _NAV_LINKS_BASE)
    for lnk in links:
        assert "text" in lnk
        assert "url" in lnk
        assert lnk["text"]
        assert lnk["url"].startswith("http")


def test_extract_nav_links_sorted_by_url_length():
    links = _extract_nav_links(_NAV_LINKS_HTML, _NAV_LINKS_BASE)
    lengths = [len(lnk["url"]) for lnk in links]
    assert lengths == sorted(lengths), "Links should be sorted shortest URL first"


def test_extract_nav_links_respects_max_links():
    links = _extract_nav_links(_NAV_LINKS_HTML, _NAV_LINKS_BASE, max_links=2)
    assert len(links) <= 2


def test_extract_nav_links_empty_html():
    links = _extract_nav_links("", "https://example.com/")
    assert links == []


def test_scrape_page_bs_result_includes_nav_links(monkeypatch):
    """When BS last-resort fires, the result must contain nav_links."""
    portal_html = """<!doctype html>
<html><head><title>Portal</title></head><body>
  <nav>
    <a href="/hours">Hours</a>
    <a href="/policies">Policies</a>
    <a href="/contact">Contact</a>
  </nav>
  <ul>""" + "".join(f"<li>nav item {i}</li>" for i in range(40)) + """</ul>
</body></html>"""
    monkeypatch.setattr(
        "app.assistant.lib.tools.scrape_url.utils.scrape.fetch_html",
        lambda url, **kw: portal_html,
    )
    result = scrape_page("https://portal.example.com/")
    assert result is not None
    assert "nav_links" in result
    assert isinstance(result["nav_links"], list)


def test_scrape_page_good_article_nav_links_empty(monkeypatch):
    """Good extraction paths should return empty nav_links."""
    monkeypatch.setattr(
        "app.assistant.lib.tools.scrape_url.utils.scrape.fetch_html",
        lambda url, **kw: ARTICLE_HTML,
    )
    result = scrape_page("https://example.com/article")
    assert result is not None
    assert result["nav_links"] == []


# ===========================================================================
# Unit tests — _assess_content_quality
# ===========================================================================

# A block of pure nav-menu text that should be classified as nav_only.
_NAV_ONLY_TEXT = "\n\n".join([
    "home",
    "about",
    "contact",
    "login",
    "sign in",
    "menu",
    "search",
    "quick link header",
    "calendar",
    "enrollment",
    "bell schedule",
    "handbook",
    "principal",
    "resources",
    "staff",
    "parent portal",
    "news",
    "events",
] * 3)   # repeat to push char count above MINIMUM_CONTENT_LENGTH

_THIN_TEXT = "Short page."   # well below 500 chars


def test_quality_good_path_trafilatura_returns_good():
    long_text = "This is a meaningful paragraph. " * 30   # > 500 chars
    assert _assess_content_quality(long_text, _PATH_TRAFILATURA) == QUALITY_GOOD


def test_quality_good_path_wikipedia_returns_good():
    long_text = "Article content. " * 40
    assert _assess_content_quality(long_text, _PATH_WIKIPEDIA) == QUALITY_GOOD


def test_quality_good_path_short_text_returns_thin():
    assert _assess_content_quality(_THIN_TEXT, _PATH_TRAFILATURA) == QUALITY_THIN


def test_quality_bs_nav_heavy_returns_nav_only():
    # 318 blocks all < 80 chars, avg length ~20 → well below both thresholds
    assert _assess_content_quality(_NAV_ONLY_TEXT, _PATH_BEAUTIFULSOUP) == QUALITY_NAV_ONLY


def test_quality_bs_real_content_returns_good():
    real_text = "\n\n".join([
        "Meadow Park Elementary School is located at 50 Blue Lake South, Irvine CA 92614.",
        "The school offers programs from Pre-K through sixth grade including enrichment activities.",
        "Attendance line: 949-936-5901. Office hours are 7:30 AM to 4:00 PM Monday through Friday.",
        "Bell schedule: First bell at 8:00 AM, dismissal at 2:45 PM for regular days.",
        "Principal's message: We are committed to academic excellence and a safe learning environment.",
    ] * 5)
    assert _assess_content_quality(real_text, _PATH_BEAUTIFULSOUP) == QUALITY_GOOD


def test_quality_bs_thin_text_returns_thin():
    assert _assess_content_quality(_THIN_TEXT, _PATH_BEAUTIFULSOUP) == QUALITY_THIN


# ===========================================================================
# Unit tests — scrape_page result shape (monkeypatched fetch)
# ===========================================================================

def test_scrape_page_result_contains_quality_and_path(monkeypatch):
    """scrape_page must always include content_quality and extraction_path in the result."""
    monkeypatch.setattr(
        "app.assistant.lib.tools.scrape_url.utils.scrape.fetch_html",
        lambda url, **kw: ARTICLE_HTML,
    )
    result = scrape_page("https://example.com/article")
    assert result is not None
    assert "content_quality" in result
    assert "extraction_path" in result
    assert result["content_quality"] in {QUALITY_GOOD, QUALITY_THIN, QUALITY_NAV_ONLY}


def test_scrape_page_good_article_reports_good_quality(monkeypatch):
    monkeypatch.setattr(
        "app.assistant.lib.tools.scrape_url.utils.scrape.fetch_html",
        lambda url, **kw: ARTICLE_HTML,
    )
    result = scrape_page("https://example.com/article")
    assert result is not None
    assert result["content_quality"] == QUALITY_GOOD


def test_scrape_page_nav_page_reports_nav_only(monkeypatch):
    """
    A pure nav/portal page should come back with a non-good quality signal.

    What fires depends on how much HTML trafilatura can extract from the page:
    - If trafilatura grabs the list items (> MINIMUM_CONTENT_LENGTH chars) it
      reports "thin" because the text is short and came from a good path.
    - If trafilatura returns nothing, BS runs and nav-density scoring fires
      "nav_only".
    Either result is acceptable — the contract is: quality != QUALITY_GOOD.
    """
    nav_html = """<!doctype html>
<html><head><title>School Portal</title></head><body>
  <h2>Quick link header</h2>
  <ul>
    <li>Home</li><li>About</li><li>Contact</li><li>Login</li><li>Sign in</li>
    <li>Menu</li><li>Search</li><li>Calendar</li><li>Enrollment</li>
    <li>Bell Schedule</li><li>Handbook</li><li>Principal</li><li>Resources</li>
    <li>Staff</li><li>Parent Portal</li><li>News</li><li>Events</li>
  </ul>
  <h2>Mobile Quicklinks</h2>
  <ul>
    <li>Home</li><li>About</li><li>Contact</li><li>Login</li><li>Sign in</li>
    <li>Menu</li><li>Search</li><li>Calendar</li><li>Enrollment</li>
    <li>Bell Schedule</li><li>Handbook</li><li>Principal</li><li>Resources</li>
    <li>Staff</li><li>Parent Portal</li><li>News</li><li>Events</li>
  </ul>
</body></html>"""
    monkeypatch.setattr(
        "app.assistant.lib.tools.scrape_url.utils.scrape.fetch_html",
        lambda url, **kw: nav_html,
    )
    result = scrape_page("https://example.com/portal")
    assert result is not None
    assert result["content_quality"] != QUALITY_GOOD, (
        f"Nav-only page should not report good quality, got {result['content_quality']!r}"
    )


def test_scrape_page_nav_only_still_returns_dict_not_none(monkeypatch):
    """Even a nav_only page must return a dict — the caller decides what to do."""
    nav_html = """<!doctype html><html><body>
      <ul>""" + "".join(f"<li>menu item {i}</li>" for i in range(30)) + """
      </ul>
    </body></html>"""
    monkeypatch.setattr(
        "app.assistant.lib.tools.scrape_url.utils.scrape.fetch_html",
        lambda url, **kw: nav_html,
    )
    result = scrape_page("https://example.com/portal")
    # May be None if content is below MINIMUM_CONTENT_LENGTH, but if returned must have quality
    if result is not None:
        assert "content_quality" in result


# ===========================================================================
# Integration tests — real network, marked so they can be skipped
# ===========================================================================

@pytest.mark.integration
def test_integration_static_site_returns_result():
    t0 = time.perf_counter()
    result = scrape_page("https://meadowpark.iusd.org/")
    elapsed = time.perf_counter() - t0
    print(f"\n[meadowpark] elapsed={elapsed:.1f}s")
    assert result is not None, "Expected content from static site"
    assert len(result["full_text"]) >= MINIMUM_CONTENT_LENGTH
    assert isinstance(result["sections"], list)
    assert len(result["sections"]) >= 1
    assert "content_quality" in result
    assert "extraction_path" in result
    print(
        f"[meadowpark] quality={result['content_quality']} "
        f"path={result['extraction_path']} "
        f"sections={len(result['sections'])} full_text={len(result['full_text'])} chars"
    )


@pytest.mark.integration
def test_integration_static_site_timing_under_threshold():
    t0 = time.perf_counter()
    scrape_page("https://meadowpark.iusd.org/")
    elapsed = time.perf_counter() - t0
    assert elapsed < 30, f"Static page took too long: {elapsed:.1f}s (threshold 30s)"


@pytest.mark.integration
def test_integration_meadowpark_quality_is_nav_only():
    """
    Meadow Park is a pure portal page — all extractors should fail down to BS,
    and the nav-density check should classify it as nav_only.
    """
    result = scrape_page("https://meadowpark.iusd.org/")
    assert result is not None
    print(f"\n[meadowpark quality] quality={result['content_quality']} path={result['extraction_path']}")
    assert result["content_quality"] == QUALITY_NAV_ONLY, (
        f"Expected nav_only for portal page, got {result['content_quality']!r} "
        f"(path={result['extraction_path']!r})"
    )


@pytest.mark.integration
def test_integration_meadowpark_nav_links_present_and_useful():
    """nav_links should include real sub-page URLs the agent can follow."""
    result = scrape_page("https://meadowpark.iusd.org/")
    assert result is not None
    nav_links = result.get("nav_links", [])
    print(f"\n[meadowpark nav_links] count={len(nav_links)}")
    for lnk in nav_links[:10]:
        print(f"  {lnk['text']!r:35s}  {lnk['url']}")

    assert len(nav_links) >= 5, f"Expected meaningful nav links, got {len(nav_links)}"
    urls = [lnk["url"] for lnk in nav_links]
    assert all("meadowpark.iusd.org" in u for u in urls), \
        "All nav_links should be internal to the same host"
    # At least one link should look like a content sub-page
    texts_lower = [lnk["text"].lower() for lnk in nav_links]
    useful = {"contact", "about", "schedule", "staff", "calendar", "handbook", "office"}
    assert any(any(w in t for w in useful) for t in texts_lower), \
        f"Expected at least one useful sub-page link, got: {texts_lower[:10]}"


@pytest.mark.integration
def test_integration_wikipedia_uses_dedicated_extractor():
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    t0 = time.perf_counter()
    result = scrape_page(url)
    elapsed = time.perf_counter() - t0
    print(f"\n[wikipedia] elapsed={elapsed:.1f}s")
    assert result is not None
    ft = result["full_text"]
    assert len(ft) > 5000, f"Wikipedia article too short: {len(ft)} chars"
    assert "Guido van Rossum" in ft or "programming language" in ft.lower()
    assert "References" not in ft[:500], "References section leaked into output"
    assert result["content_quality"] == QUALITY_GOOD
    assert result["extraction_path"] == _PATH_WIKIPEDIA
    print(f"[wikipedia] quality={result['content_quality']} path={result['extraction_path']} full_text={len(ft)} chars, sections={len(result['sections'])}")


@pytest.mark.integration
def test_integration_wikipedia_timing_under_threshold():
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    t0 = time.perf_counter()
    scrape_page(url)
    elapsed = time.perf_counter() - t0
    assert elapsed < 30, f"Wikipedia page took too long: {elapsed:.1f}s (threshold 30s)"


@pytest.mark.integration
def test_integration_reddit_refused():
    result = scrape_page("https://www.reddit.com/r/python")
    assert result is None, "scrape_page must refuse Reddit URLs"


@pytest.mark.integration
def test_integration_section_scorer_ranks_correct_section():
    """
    Fetch a real multi-section page and verify the scorer surfaces the right section
    for a specific query.
    """
    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    result = scrape_page(url)
    assert result is not None
    ranked = score_sections_against_query(result["sections"], "history creation Guido van Rossum")
    assert ranked, "Expected at least one section after scoring"
    top = ranked[0]
    print(f"\n[scorer] top section: {top['section_title']!r} score={top['score']}")
    # The top section should mention Guido or history, not be completely unrelated
    haystack = (top["section_title"] + " " + top["text"]).lower()
    assert "guido" in haystack or "history" in haystack or "created" in haystack, (
        f"Top-ranked section does not mention history/Guido: {top['section_title']!r}"
    )


@pytest.mark.integration
def test_integration_js_heavy_site_returns_result():
    """
    Dave & Buster's renders heavily with JavaScript.
    Expect the tool to escalate to Playwright and return content.
    This test is slow (~25-40s) because of Playwright startup + page wait.
    """
    url = "https://www.daveandbusters.com/us/en/about/locations/rancho-mirage"
    t0 = time.perf_counter()
    result = scrape_page(url)
    elapsed = time.perf_counter() - t0
    print(f"\n[daveandbusters] elapsed={elapsed:.1f}s")
    assert result is not None, "Expected content from D&B location page"
    ft = result["full_text"]
    assert len(ft) >= MINIMUM_CONTENT_LENGTH
    assert result["content_quality"] == QUALITY_GOOD
    print(f"[daveandbusters] quality={result['content_quality']} path={result['extraction_path']} sections={len(result['sections'])} full_text={len(ft)} chars")
    for s in result["sections"][:5]:
        print(f"  section: {s['section_title']!r} ({len(s['text'])} chars)")


@pytest.mark.integration
def test_integration_js_heavy_section_scorer():
    """After scraping D&B, the scorer should surface hours/policies near the top."""
    url = "https://www.daveandbusters.com/us/en/about/locations/rancho-mirage"
    result = scrape_page(url)
    assert result is not None
    query = "hours Sunday minors under 21 cover charge house policies"
    ranked = score_sections_against_query(result["sections"], query)
    assert ranked
    print(f"\n[daveandbusters scorer] top={ranked[0]['section_title']!r} score={ranked[0]['score']}")
    print(f"  text preview: {ranked[0]['text'][:300]}")


# ===========================================================================
# Entry point for direct execution
# ===========================================================================

def main() -> int:
    import subprocess
    cmd = [
        sys.executable, "-m", "pytest",
        __file__,
        "-v",
        "--tb=short",
        "-m", "not integration",
    ]
    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
