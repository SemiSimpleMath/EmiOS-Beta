"""Shared harness for live Playwright MCP browser tests.

These tests drive the real web_* tools against a real browser. They launch
their OWN Playwright MCP server: headless, `--isolated` (throwaway in-memory
profile), no window-position config. The production launch option
(mcp/servers/npm/playwright-mcp.yaml) uses the assistant's persistent Chrome
profile, which a running EmiOS instance holds, so tests must never use it.

Page content is driven with `browser_evaluate` (page-context JS),
`browser_resize` and `browser_mouse_*_xy`. playwright-mcp removed
`browser_run_code` (Playwright-side `async (page) => ...`) in its 2026
upgrade; the production tools moved to `browser_evaluate` at the same time.

Runtime requirements (checked by `requires_live_playwright`):
- `npx` on PATH (launches `@playwright/mcp@latest`; first run downloads it)
- Google Chrome stable installed (`--browser=chrome`, as in production)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import app.assistant.tests.test_setup  # noqa: F401 (side-effect: initializes DI)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.mcp.tool_runner import _close_stdio_session
from app.assistant.lib.tools.web_mcp_utils import mcp_call
from app.assistant.utils.json_parsing import parse_jsonish

SERVER_ID = "npm/playwright-mcp"

_TEST_LAUNCH_OPTION = {
    "id": "test_isolated_headless",
    "transport": "stdio",
    "command": "cmd" if sys.platform == "win32" else "npx",
    "args": (["/c", "npx"] if sys.platform == "win32" else [])
    + ["-y", "@playwright/mcp@latest", "--browser=chrome", "--caps=vision", "--headless", "--isolated"],
}


def _chrome_installed() -> bool:
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")]
        candidates = [Path(r) / "Google" / "Chrome" / "Application" / "chrome.exe" for r in roots if r]
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    else:
        candidates = [Path("/opt/google/chrome/chrome")]
    return any(p.is_file() for p in candidates)


def _missing_requirements() -> list[str]:
    missing = []
    if shutil.which("npx") is None:
        missing.append("`npx` not on PATH (needed to launch @playwright/mcp)")
    if not _chrome_installed():
        missing.append("Google Chrome stable not installed (Playwright MCP runs with --browser=chrome)")
    return missing


_MISSING = _missing_requirements()

requires_live_playwright = pytest.mark.skipif(
    bool(_MISSING),
    reason="Live Playwright MCP browser test: " + "; ".join(_MISSING),
)


@pytest.fixture(scope="module")
def live_server_entry():
    """The registered playwright-mcp server entry, relaunched isolated + headless.

    The web_* tools resolve the same dict via DI.tool_registry, so they talk to
    the same test browser session. The original launch options are restored and
    the test session closed at module teardown.
    """
    entry = DI.tool_registry.get_mcp_server_entry(SERVER_ID)
    assert isinstance(entry, dict), f"Missing MCP server entry {SERVER_ID}"
    original = entry["launch_options"]
    _close_stdio_session(SERVER_ID)
    entry["launch_options"] = [dict(_TEST_LAUNCH_OPTION)]
    try:
        yield entry
    finally:
        _close_stdio_session(SERVER_ID)
        entry["launch_options"] = original


def evaluate(server_entry: dict, fn_js: str) -> Any:
    """Run a page-context function (`() => ...`) via browser_evaluate; return parsed JSON."""
    text, is_error, _ = mcp_call(server_entry=server_entry, tool_name="browser_evaluate", arguments={"function": fn_js})
    assert not is_error, f"browser_evaluate failed: {text}"
    return parse_jsonish(text)


def resize(server_entry: dict, width: int, height: int) -> None:
    text, is_error, _ = mcp_call(
        server_entry=server_entry, tool_name="browser_resize", arguments={"width": int(width), "height": int(height)}
    )
    assert not is_error, f"browser_resize failed: {text}"


def load_page(server_entry: dict, html: str, width: int, height: int) -> None:
    """Set the viewport, then replace the current document (inline scripts run)."""
    resize(server_entry, width, height)
    evaluate(
        server_entry,
        "() => { document.open(); document.write(%s); document.close(); return { ok: true }; }" % json.dumps(html),
    )


def click_xy(server_entry: dict, x: float, y: float) -> None:
    text, is_error, _ = mcp_call(
        server_entry=server_entry,
        tool_name="browser_mouse_click_xy",
        arguments={"x": float(x), "y": float(y), "element": "test click target"},
    )
    assert not is_error, f"browser_mouse_click_xy failed: {text}"


def element_at(server_entry: dict, x: float, y: float) -> dict:
    """DOM info for the element at (x, y) via elementFromPoint."""
    js = """
() => {
  const el = document.elementFromPoint(%s, %s);
  if (!el) return { found: false };
  const a = el.closest ? el.closest('a') : null;
  return {
    found: true,
    tag: (el.tagName || "").toLowerCase(),
    id: el.id || null,
    aria: el.getAttribute('aria-label') || null,
    href: a ? (a.getAttribute('href') || null) : null,
    text: (el.innerText || el.textContent || '').trim().slice(0, 60),
  };
}
""" % (float(x), float(y))
    result = evaluate(server_entry, js)
    return result if isinstance(result, dict) else {}


def live_marks_map(server_entry: dict) -> list[dict]:
    """window.__emi_marks_map as it stands now.

    web_page_coords writes it during injection and clears it when it removes the
    overlay before returning, so read it while the tool is running (from a
    stubbed vision agent), not after.
    """
    js = "() => (Array.isArray(window.__emi_marks_map) ? window.__emi_marks_map : [])"
    result = evaluate(server_entry, js)
    return result if isinstance(result, list) else []
