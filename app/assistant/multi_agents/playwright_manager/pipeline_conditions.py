import re

from app.assistant.utils.pipeline_state import get_flag


def _extract_open_tabs_from_mcp_markdown(text: str) -> tuple[list[dict], int | None]:
    """
    Parse Playwright MCP markdown that contains a section like:

      ### Open tabs
      - 0: (current) [Title](https://...)
      - 1: [](about:blank)

    Returns (tabs, current_index).
    tabs: [{index:int, title:str|None, url:str|None, current:bool}]
    """
    if not isinstance(text, str) or "Open tabs" not in text:
        return [], None

    lines = text.splitlines()
    in_tabs = False
    tabs: list[dict] = []
    current_idx: int | None = None

    # Matches:
    # - 2: (current) [Some title](https://example)
    # - 1: [](about:blank)
    pat = re.compile(r"^\s*[-*]\s*(\d+):\s*(?:\((current)\)\s*)?\[(.*?)\]\((.*?)\)\s*$")
    for ln in lines:
        if ln.strip().startswith("### Open tabs"):
            in_tabs = True
            continue
        if not in_tabs:
            continue
        if ln.strip().startswith("### "):
            break
        m = pat.match(ln.strip())
        if not m:
            continue
        idx = int(m.group(1))
        is_current = bool(m.group(2))
        title = (m.group(3) or "").strip() or None
        url = (m.group(4) or "").strip() or None
        tabs.append({"index": idx, "title": title, "url": url, "current": is_current})
        if is_current:
            current_idx = idx
    return tabs, current_idx


def _extract_page_url_from_mcp_markdown(text: str) -> str | None:
    """
    Extract the *latest* "- Page URL:" line from Playwright MCP markdown.

    Playwright MCP often emits scheme-less URLs like "//www.doordash.com/...".
    Normalize those to "https://..." so comparisons against the "Open tabs" URLs work.
    """
    if not isinstance(text, str):
        return None

    url: str | None = None
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("- Page URL:"):
            u = s.split(":", 2)[-1].strip()
            if u:
                url = u

    if not url:
        return None

    if url.startswith("//"):
        url = "https:" + url
    return url or None


def new_tab_right_of_current(raw_content: str | None, blackboard) -> dict | None:
    tabs, current_idx = _extract_open_tabs_from_mcp_markdown(raw_content or "")
    if not tabs or current_idx is None:
        return None

    def _norm_url(u: str | None) -> str | None:
        if not isinstance(u, str):
            return None
        s = u.strip()
        if not s:
            return None
        if s.startswith("//"):
            s = "https:" + s
        return s

    page_url = _norm_url(_extract_page_url_from_mcp_markdown(raw_content or ""))
    best: dict | None = None
    for t in tabs:
        try:
            if not isinstance(t, dict):
                continue
            idx = t.get("index")
            if not isinstance(idx, int):
                idx = int(idx)
            if idx <= int(current_idx):
                continue
            url = _norm_url(t.get("url"))
            if not isinstance(url, str) or not url.strip():
                continue
            if url.strip() == "about:blank":
                continue
            if isinstance(page_url, str) and page_url.strip() and url.strip() == page_url.strip():
                continue
            if best is None or int(idx) > int(best.get("index", -1)):
                best = t
        except Exception:
            continue
    if not best:
        return None
    return {
        "new_tab_index": int(best["index"]),
        "new_tab_url": best.get("url"),
        "new_tab_title": best.get("title"),
        "current_tab_index": int(current_idx),
    }


def auto_scan_in_progress(raw_content: str | None, blackboard) -> dict | None:  # noqa: ARG001
    if bool(get_flag(blackboard, "playwright_auto_scan_in_progress", False)):
        return {}
    return None
