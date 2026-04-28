"""
Tests for web_page_coords under browser_resize / viewport changes.

Three concrete questions being answered:

  Q1. Is browser_resize actually helpful?
      Does enlarging the viewport reveal more clickable elements that were
      previously below the fold (outside the smaller viewport)?

  Q2. Does browser_resize break web_page_coords click accuracy?
      After browser_resize, the tool must still return coords that correctly
      identify elements — especially near viewport edges where drift is worst.
      We verify by clicking the returned coords and checking the DOM result.

  Q3. What happens to the screenshot when resolution changes?
      The PNG produced by web_page_coords must have dimensions that match
      the current viewport. A mismatch means the vision model is looking at
      an image whose pixel coordinates do not correspond to DOM coordinates —
      making its badge-number picks wrong or, if they happen to be right,
      the fetched coords land in the wrong screen location.

All tests call web_page_coords (the real tool) end-to-end.
The vision_mark_picker agent is stubbed so picks are deterministic and free.
Stubs label-match against window.__emi_marks_map (live browser state) to
simulate a correct vision decision — so the pipeline is:
  inject marks → screenshot → stub picks badge N by label → fetch coords → verify
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "app").is_dir():
        sys.path.insert(0, str(_parent))
        break

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


# ---------------------------------------------------------------------------
# Helpers — mirror the extraction pattern from test_web_page_coords_marks.py
# ---------------------------------------------------------------------------

def _parse_jsonish(text: str) -> Any:
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    # Strip the ### Result / ### Ran Playwright code envelope that MCP adds.
    if s.startswith("### Result"):
        lines = s.split("\n", 1)
        if len(lines) > 1:
            rest = lines[1].strip()
            # Content ends at the next ### block.
            end = rest.find("\n###")
            if end >= 0:
                rest = rest[:end].strip()
            s = rest
    try:
        return json.loads(s)
    except Exception:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s.lstrip())
            return obj
        except Exception:
            pass
    m = re.search(r"```json\s*([\s\S]*?)```", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"```\s*([\s\S]*?)```", s)
    if m:
        payload = (m.group(1) or "").strip()
        try:
            return json.loads(payload)
        except Exception:
            try:
                obj, _ = json.JSONDecoder().raw_decode(payload.lstrip())
                return obj
            except Exception:
                pass
    i1 = min([i for i in [s.find("["), s.find("{"), s.find('"')] if i >= 0] or [-1])
    if i1 >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[i1:].lstrip())
            return obj
        except Exception:
            pass
    return None


def _mcp_run(server_entry: dict, js: str, timeout_s: float = 20) -> Any:
    """Run browser_run_code and parse the result JSON."""
    resp = mcp_stdio_call_tool(
        server_entry=server_entry,
        tool_name="browser_run_code",
        arguments={"code": js},
        timeout_s=timeout_s,
    )
    text_items = ((resp or {}).get("result") or {}).get("content") or []
    text = ""
    for it in text_items:
        if isinstance(it, dict) and it.get("type") == "text":
            text = it.get("text") or ""
            break
    return _parse_jsonish(text)


def _refresh_playwright_mcp_tool_cache(repo_root: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(repo_root / "mcp" / "refresh_tool_cache.py"),
         "--server-id", "npm/playwright-mcp", "--launch-id", "cmd_npx", "--timeout", "60"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to refresh Playwright MCP tool cache.\n"
            + f"exit={proc.returncode}\n"
            + (f"[stdout]\n{proc.stdout}" if proc.stdout else "")
            + (f"[stderr]\n{proc.stderr}" if proc.stderr else "")
        )


def _ensure_playwright_mcp_tools() -> None:
    required = [
        "mcp::npm/playwright-mcp::browser_run_code",
        "mcp::npm/playwright-mcp::browser_take_screenshot",
        "mcp::npm/playwright-mcp::browser_mouse_click_xy",
        "mcp::npm/playwright-mcp::browser_resize",
    ]
    missing = [t for t in required if not DI.tool_registry.get_tool(t)]
    if not missing:
        return
    repo_root = Path(__file__).resolve().parents[3]
    _refresh_playwright_mcp_tool_cache(repo_root)
    DI.tool_registry.load_mcp_tool_cache(enabled_only=True)
    still_missing = [t for t in required if not DI.tool_registry.get_tool(t)]
    if still_missing:
        raise RuntimeError(
            "Missing Playwright MCP tools after refresh:\n"
            + "\n".join(f"  {t}" for t in still_missing)
        )


def _browser_resize(server_entry: dict, width: int, height: int) -> None:
    """Call browser_resize MCP tool — exactly what playwright_page_overview does."""
    mcp_stdio_call_tool(
        server_entry=server_entry,
        tool_name="browser_resize",
        arguments={"width": width, "height": height},
        timeout_s=15,
    )


def _get_viewport(server_entry: dict) -> dict:
    js = """
async (page) => {
  return await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight }));
}
"""
    return _mcp_run(server_entry, js, timeout_s=10) or {}


def _get_marks_map(server_entry: dict) -> list[dict]:
    """Fetch window.__emi_marks_map from the current page."""
    js = """
async (page) => {
  return await page.evaluate(() => {
    return (window.__emi_marks_map && Array.isArray(window.__emi_marks_map))
      ? window.__emi_marks_map : [];
  });
}
"""
    result = _mcp_run(server_entry, js) or []
    return result if isinstance(result, list) else []


def _element_at(server_entry: dict, x: float, y: float) -> dict:
    """Return DOM info for the element at (x, y) via elementFromPoint."""
    js = f"""
async (page) => {{
  const x = {x};
  const y = {y};
  return await page.evaluate(({{x, y}}) => {{
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    return {{
      found: true,
      tag: (el.tagName || "").toLowerCase(),
      id: el.id || null,
      aria: el.getAttribute('aria-label') || null,
      testid: el.getAttribute('data-testid') || null,
      text: (el.innerText || el.textContent || '').trim().slice(0, 60),
    }};
  }}, {{x, y}});
}}
"""
    resp = mcp_stdio_call_tool(
        server_entry=server_entry,
        tool_name="browser_run_code",
        arguments={"code": js},
        timeout_s=20,
    )
    text_items = ((resp or {}).get("result") or {}).get("content") or []
    text = ""
    for it in text_items:
        if isinstance(it, dict) and it.get("type") == "text":
            text = it.get("text") or ""
            break
    result = _parse_jsonish(text)
    return result if isinstance(result, dict) else {}


def _remove_marks_overlay(server_entry: dict) -> None:
    """Remove the numbered overlay badges injected by web_page_coords so they don't intercept clicks."""
    js = """
async (page) => {
  await page.evaluate(() => {
    const container = document.getElementById('__emi_marks_container');
    if (container) container.remove();
  });
  return { ok: true };
}
"""
    _mcp_run(server_entry, js, timeout_s=10)


def _click_xy(server_entry: dict, x: float, y: float) -> None:
    """Click at CSS pixel coords (x, y) via page.mouse.click() — same coordinate space as DOM."""
    js = f"""
async (page) => {{
  await page.mouse.click({x}, {y});
  return {{ ok: true }};
}}
"""
    _mcp_run(server_entry, js, timeout_s=15)


def _get_png_dimensions(path: str) -> tuple[int, int]:
    """Read PNG width/height from the file header without importing Pillow."""
    import struct
    with open(path, "rb") as f:
        sig = f.read(8)
        assert sig == b"\x89PNG\r\n\x1a\n", f"Not a PNG file: {path}"
        f.read(4)   # chunk length
        f.read(4)   # IHDR
        w = struct.unpack(">I", f.read(4))[0]
        h = struct.unpack(">I", f.read(4))[0]
    return w, h


# ---------------------------------------------------------------------------
# Vision stub — label-matches against live window.__emi_marks_map
# ---------------------------------------------------------------------------

class _StubPickMarkByLabel:
    """
    Simulates vision_mark_picker deterministically:
    reads the live marks map, finds the mark whose label contains target_substr,
    returns that mark id.  Falls back to mark id 1.

    This exercises the full pipeline (inject→screenshot→pick→fetch→return coords)
    without an OpenAI call.
    """
    def __init__(self, server_entry: dict, target_substr: str):
        self._server_entry = server_entry
        self._target = target_substr.lower()

    def action_handler(self, _msg):
        marks = _get_marks_map(self._server_entry)
        chosen_id = 1
        for m in marks:
            if self._target in str(m.get("label") or "").lower():
                chosen_id = int(m["id"])
                break
        return ToolResult(
            result_type="llm_result",
            content=f"stub: mark {chosen_id} for {self._target!r}",
            data={"action": "done", "mark_ids": [chosen_id], "confidence": 1.0, "rationale": "label match"},
        )


def _patch_vision(orig, server_entry: dict, target_substr: str):
    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_mark_picker":
            return _StubPickMarkByLabel(server_entry, target_substr)
        return orig(name, blackboard=blackboard)
    return _create_agent


def _run_web_page_coords(question: str) -> ToolResult:
    """Call the real web_page_coords tool end-to-end."""
    tool_cfg = DI.tool_registry.get_tool("web_page_coords")
    assert tool_cfg and tool_cfg.get("tool_class"), "web_page_coords not registered"
    return tool_cfg["tool_class"]().execute(
        ToolMessage(
            tool_name="web_page_coords",
            tool_data={"tool_name": "web_page_coords",
                       "arguments": {"question": question, "strict": True}},
        )
    )


# ---------------------------------------------------------------------------
# Test page: a tall scrollable page with one below-fold button
# ---------------------------------------------------------------------------

_TALL_PAGE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Tall Page Test</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; }
    .spacer { height: 1200px; background: linear-gradient(#e2e8f0, #cbd5e1);
              display: flex; align-items: center; justify-content: center;
              font-size: 24px; color: #475569; }
    #above-fold { position: fixed; top: 20px; left: 50%;
                  transform: translateX(-50%);
                  padding: 14px 28px; background: #3b82f6; color: #fff;
                  border: none; border-radius: 10px; font-size: 16px; font-weight: 700;
                  cursor: pointer; }
    #below-fold { position: absolute; top: 1400px; left: 50%;
                  transform: translateX(-50%);
                  padding: 14px 28px; background: #22c55e; color: #fff;
                  border: none; border-radius: 10px; font-size: 16px; font-weight: 700;
                  cursor: pointer; }
    #clicked-result { position: fixed; bottom: 20px; left: 20px;
                      font-weight: 700; font-size: 18px; color: #166534; }
  </style>
</head>
<body>
  <button id="above-fold" aria-label="Above Fold Button">Above Fold</button>
  <div class="spacer">Scroll down to find the green button</div>
  <button id="below-fold" aria-label="Below Fold Button">Below Fold</button>
  <div id="clicked-result"></div>
  <script>
    document.getElementById('above-fold').addEventListener('click', () => {
      document.getElementById('clicked-result').textContent = 'clicked:above-fold';
    });
    document.getElementById('below-fold').addEventListener('click', () => {
      document.getElementById('clicked-result').textContent = 'clicked:below-fold';
    });
  </script>
</body>
</html>
"""


def _load_tall_page(server_entry: dict, vw: int, vh: int) -> None:
    js = f"""
async (page) => {{
  await page.setViewportSize({{ width: {vw}, height: {vh} }});
  await page.setContent({json.dumps(_TALL_PAGE_HTML)}, {{ waitUntil: 'domcontentloaded' }});
  return {{ ok: true }};
}}
"""
    _mcp_run(server_entry, js, timeout_s=30)


def _get_clicked_result(server_entry: dict) -> str:
    js = """
async (page) => {
  return await page.evaluate(() => {
    const el = document.getElementById('clicked-result');
    return el ? (el.textContent || '') : '';
  });
}
"""
    return str(_mcp_run(server_entry, js) or "")


# ---------------------------------------------------------------------------
# Q1: Is browser_resize actually helpful?
#     Does enlarging the viewport reveal more buttons?
# ---------------------------------------------------------------------------

def test_q1_browser_resize_reveals_below_fold_button():
    """
    Q1: Is browser_resize helpful?

    Page layout: 'Above Fold' button is always visible (position:fixed).
    'Below Fold' button is at y=1400px — invisible at a small viewport (800 tall)
    but visible once the viewport is enlarged past 1400px.

    At small viewport: web_page_coords should find only 1 button (above fold).
    After browser_resize to tall viewport: should find 2 buttons (both visible).

    This directly answers whether enlarging the viewport gives the agent access
    to more of the page without scrolling.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    # --- Small viewport: below-fold button is off screen ---
    _load_tall_page(server_entry, vw=1280, vh=800)
    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "above fold")
    try:
        res_small = _run_web_page_coords("Find buttons on the page")
    finally:
        DI.agent_factory.create_agent = orig

    marks_small = _get_marks_map(server_entry)
    labels_small = [str(m.get("label") or "").lower() for m in marks_small]
    above_found_small = any("above fold" in l for l in labels_small)
    below_found_small = any("below fold" in l for l in labels_small)

    assert above_found_small, f"Above Fold button not found at small viewport. Labels: {labels_small}"
    # Below-fold button is at y=1400 — outside 800-tall viewport, should not be in marks.
    assert not below_found_small, (
        f"Below Fold button unexpectedly found at small viewport (800px tall).\n"
        f"Labels: {labels_small}\n"
        f"This means the button was already visible — adjust _TALL_PAGE_HTML top value."
    )

    # --- Enlarge with browser_resize (exactly as playwright_page_overview does) ---
    _browser_resize(server_entry, width=1280, height=1600)

    vp = _get_viewport(server_entry)
    assert int(vp.get("height") or 0) >= 1500, f"browser_resize did not enlarge. got={vp}"

    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "below fold")
    try:
        res_large = _run_web_page_coords("Find buttons on the page")
    finally:
        DI.agent_factory.create_agent = orig

    marks_large = _get_marks_map(server_entry)
    labels_large = [str(m.get("label") or "").lower() for m in marks_large]
    below_found_large = any("below fold" in l for l in labels_large)

    assert below_found_large, (
        f"Below Fold button NOT found after browser_resize to 1600px tall.\n"
        f"Labels seen: {labels_large}\n"
        f"Answer to Q1: browser_resize did NOT help reveal the below-fold button."
    )
    print(
        f"\n[Q1 RESULT] browser_resize IS helpful: "
        f"small({len(marks_small)} marks, below-fold={below_found_small}) → "
        f"large({len(marks_large)} marks, below-fold={below_found_large})"
    )


# ---------------------------------------------------------------------------
# Q2: Does browser_resize break web_page_coords accuracy?
#     Test with a button at each corner — especially bottom-right (worst case).
# ---------------------------------------------------------------------------

_CORNER_BUTTONS_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Corner Buttons</title>
  <style>
    body { margin: 0; background: #f0f4f8; }
    .btn {
      position: fixed; width: 120px; height: 44px;
      border-radius: 8px; border: 2px solid #334155;
      background: #fff; font-weight: 700; cursor: pointer;
    }
    #clicked-result { position: fixed; top: 50%; left: 50%;
                      transform: translate(-50%,-50%);
                      font-size: 24px; font-weight: 700;
                      background: rgba(255,255,255,0.9);
                      padding: 16px 24px; border-radius: 12px; }
  </style>
</head>
<body>
  <button id="btn-tl" class="btn" style="top:10px;left:10px;"
    aria-label="Top Left Button">TL</button>
  <button id="btn-br" class="btn" style="bottom:10px;right:10px;"
    aria-label="Bottom Right Button">BR</button>
  <div id="clicked-result">-</div>
  <script>
    ['btn-tl', 'btn-br'].forEach(id => {
      document.getElementById(id).addEventListener('click', () => {
        document.getElementById('clicked-result').textContent = 'clicked:' + id;
      });
    });
  </script>
</body>
</html>
"""


def _load_corner_page(server_entry: dict, vw: int, vh: int) -> None:
    js = f"""
async (page) => {{
  await page.setViewportSize({{ width: {vw}, height: {vh} }});
  await page.setContent({json.dumps(_CORNER_BUTTONS_HTML)}, {{ waitUntil: 'domcontentloaded' }});
  return {{ ok: true }};
}}
"""
    _mcp_run(server_entry, js, timeout_s=30)


def _get_click_result(server_entry: dict) -> str:
    js = """
async (page) => {
  return await page.evaluate(() => {
    const el = document.getElementById('clicked-result');
    return el ? (el.textContent || '') : 'ELEMENT_NOT_FOUND';
  });
}
"""
    return str(_mcp_run(server_entry, js) or "")


def test_q2_web_page_coords_click_accuracy_before_browser_resize():
    """
    Q2 baseline: web_page_coords returns accurate coords at default viewport.
    We call web_page_coords for the Bottom Right button, then actually click
    the returned coords and verify the click registered on the correct button.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    _load_corner_page(server_entry, 1280, 800)

    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "bottom right")
    try:
        res = _run_web_page_coords("Find the Bottom Right Button")
    finally:
        DI.agent_factory.create_agent = orig

    assert res.data.get("marked") is True
    targets = res.data.get("targets") or []
    assert targets, "No targets from web_page_coords at 1280x800"
    x, y = float(targets[0]["x"]), float(targets[0]["y"])

    # Verify coords via elementFromPoint.
    info = _element_at(server_entry, x, y)
    assert info.get("found"), f"web_page_coords returned ({x},{y}) but elementFromPoint found nothing"
    assert (
        info.get("id") == "btn-br"
        or "bottom right" in str(info.get("aria") or "").lower()
    ), f"Baseline: web_page_coords aimed at wrong element at ({x},{y}): {info!r}"

    # Actually click and verify the DOM click event fired.
    _remove_marks_overlay(server_entry)
    _click_xy(server_entry, x, y)
    result = _get_click_result(server_entry)
    assert result == "clicked:btn-br", (
        f"Baseline click at ({x},{y}) registered '{result}' instead of 'clicked:btn-br'.\n"
        f"element at coords: {info!r}"
    )


def test_q2_web_page_coords_click_accuracy_after_browser_resize():
    """
    Q2 core: does browser_resize break click accuracy?

    Steps:
      1. Load page at 1280x800.
      2. browser_resize to 2048x1200 (as playwright_page_overview does).
      3. Call web_page_coords for the Bottom Right button.
      4. Click the returned coords.
      5. Verify the click registered on btn-br (not some other element).

    The Bottom Right button is at position:fixed bottom:10px right:10px.
    After resize it moves to (2048-70, 1200-32) = (1978, 1168).
    If web_page_coords recomputes coords correctly for the resized viewport,
    the click lands on btn-br. If it uses stale or wrong coords, it misses.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    _load_corner_page(server_entry, 1280, 800)
    _browser_resize(server_entry, width=2048, height=1200)

    vp = _get_viewport(server_entry)
    resized_w = int(vp.get("width") or 0)
    resized_h = int(vp.get("height") or 0)
    assert resized_w >= 1400, f"browser_resize did not take effect. viewport={vp}"

    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "bottom right")
    try:
        res = _run_web_page_coords("Find the Bottom Right Button")
    finally:
        DI.agent_factory.create_agent = orig

    assert res.data.get("marked") is True, "No marks found after browser_resize"
    targets = res.data.get("targets") or []
    assert targets, "web_page_coords returned no targets after browser_resize"
    x, y = float(targets[0]["x"]), float(targets[0]["y"])

    # Numerical sanity: coords should be near the resized BR corner.
    expected_x = resized_w - 70
    expected_y = resized_h - 32
    assert abs(x - expected_x) <= 40, (
        f"After browser_resize({resized_w}x{resized_h}), BR x={x} but expected ~{expected_x}.\n"
        f"Diff={abs(x - expected_x):.0f}px. web_page_coords may have used stale coords."
    )
    assert abs(y - expected_y) <= 40, (
        f"After browser_resize({resized_w}x{resized_h}), BR y={y} but expected ~{expected_y}.\n"
        f"Diff={abs(y - expected_y):.0f}px."
    )

    # Click and verify — this is the real test: does the click land correctly?
    _remove_marks_overlay(server_entry)
    _click_xy(server_entry, x, y)
    result = _get_click_result(server_entry)
    assert result == "clicked:btn-br", (
        f"After browser_resize({resized_w}x{resized_h}): click at ({x},{y}) "
        f"registered '{result}' instead of 'clicked:btn-br'.\n"
        f"Answer to Q2: browser_resize DOES break web_page_coords accuracy."
    )
    print(
        f"\n[Q2 RESULT] browser_resize does NOT break accuracy: "
        f"coords=({x},{y}), click='{result}', viewport={resized_w}x{resized_h}"
    )


def test_q2_top_left_button_click_accurate_before_and_after_browser_resize():
    """
    Q2 near-origin: the Top Left button (top:10px left:10px) should always
    be near (70, 32). Verify it still clicks correctly after browser_resize.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    for label, setup in [
        ("before resize", lambda: _load_corner_page(server_entry, 1280, 800)),
        ("after resize", lambda: (
            _load_corner_page(server_entry, 1280, 800),
            _browser_resize(server_entry, 2048, 1200)
        )),
    ]:
        setup()

        orig = DI.agent_factory.create_agent
        DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "top left")
        try:
            res = _run_web_page_coords("Find the Top Left Button")
        finally:
            DI.agent_factory.create_agent = orig

        targets = res.data.get("targets") or []
        assert targets, f"No targets for Top Left button {label}"
        x, y = float(targets[0]["x"]), float(targets[0]["y"])

        assert abs(x - 70) <= 25, f"TL x={x} not near 70 ({label})"
        assert abs(y - 32) <= 25, f"TL y={y} not near 32 ({label})"

        _remove_marks_overlay(server_entry)
        _click_xy(server_entry, x, y)
        result = _get_click_result(server_entry)
        assert result == "clicked:btn-tl", (
            f"Top Left click at ({x},{y}) hit '{result}' {label}.\n"
            f"Expected 'clicked:btn-tl'."
        )


# ---------------------------------------------------------------------------
# Q3: What happens to the screenshot when resolution changes?
#     PNG dimensions must match viewport — mismatch = wrong coordinates.
# ---------------------------------------------------------------------------

def test_q3_screenshot_dimensions_match_viewport_at_default_size():
    """
    Q3 baseline: PNG dimensions = viewport dimensions at default 1280x800.

    web_page_coords captures a screenshot as part of its pipeline.
    The PNG width × height must equal window.innerWidth × window.innerHeight.
    If they don't match, the vision model sees badge N at pixel (px, py) in
    the image, but the DOM coords in window.__emi_marks_map were computed in
    CSS pixels — the two coordinate systems are misaligned.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    _load_corner_page(server_entry, 1280, 800)
    vp = _get_viewport(server_entry)
    vp_w = int(vp.get("width") or 0)
    vp_h = int(vp.get("height") or 0)

    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "top left")
    try:
        res = _run_web_page_coords("Find the Top Left Button")
    finally:
        DI.agent_factory.create_agent = orig

    image_path = res.data.get("image_path")
    assert image_path and Path(image_path).exists(), f"No screenshot saved. data={res.data!r}"

    png_w, png_h = _get_png_dimensions(image_path)
    assert png_w == vp_w, (
        f"PNG width={png_w} != viewport width={vp_w} at default size.\n"
        f"A mismatch means DOM coords and screenshot pixels are misaligned."
    )
    assert png_h == vp_h, (
        f"PNG height={png_h} != viewport height={vp_h} at default size."
    )
    print(f"\n[Q3 baseline] PNG={png_w}x{png_h}, viewport={vp_w}x{vp_h} — MATCH")


def test_q3_screenshot_dimensions_match_viewport_after_browser_resize():
    """
    Q3 core: after browser_resize, does the screenshot still match the viewport?

    browser_resize changes the browser window size. The screenshot should then
    be taken at the new (larger) dimensions. If the PNG is still 1280x800 but
    the viewport is now 2048x1200, the vision model's pixel coordinates for badge
    positions will be wrong relative to DOM getBoundingClientRect values.

    This is the root cause the user reported: "changed screen resolution so we
    can see more, but this messed up our ability to pick accurate coords,
    particularly near edges."
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    _load_corner_page(server_entry, 1280, 800)
    _browser_resize(server_entry, width=2048, height=1200)

    vp = _get_viewport(server_entry)
    vp_w = int(vp.get("width") or 0)
    vp_h = int(vp.get("height") or 0)
    assert vp_w >= 1400, f"browser_resize did not take effect. viewport={vp}"

    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "bottom right")
    try:
        res = _run_web_page_coords("Find the Bottom Right Button")
    finally:
        DI.agent_factory.create_agent = orig

    image_path = res.data.get("image_path")
    assert image_path and Path(image_path).exists(), f"No screenshot saved. data={res.data!r}"

    png_w, png_h = _get_png_dimensions(image_path)

    print(
        f"\n[Q3 after browser_resize] PNG={png_w}x{png_h}, viewport={vp_w}x{vp_h}"
        + (" — MATCH" if png_w == vp_w and png_h == vp_h else " — MISMATCH ⚠️")
    )

    # This assertion documents the expected behaviour.
    # If it fails: PNG is smaller than the viewport — screenshot was captured before
    # resize fully settled, or browser_resize uses a different coordinate space.
    # That would explain the edge-coord inaccuracy the user reported.
    assert png_w == vp_w, (
        f"PNG width={png_w} != viewport width={vp_w} after browser_resize.\n"
        f"The screenshot is capturing a smaller region than the browser is displaying.\n"
        f"Vision model sees badge near px={png_w - 50} but DOM coord for the same\n"
        f"button is at {vp_w - 70}px — they do not correspond."
    )
    assert png_h == vp_h, (
        f"PNG height={png_h} != viewport height={vp_h} after browser_resize.\n"
        f"Screenshot/DOM coordinate mismatch — edge buttons will have wrong coords."
    )


def test_q3_screenshot_dimensions_at_multiple_viewport_sizes():
    """
    Q3 parametric: verify PNG dimensions == viewport dimensions at several sizes.
    Also captures the screenshot path so you can visually inspect what the
    vision model actually sees at each resolution.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    sizes = [
        (1280, 800,  "small baseline"),
        (1920, 1080, "standard HD"),
        (2560, 1400, "large/wide"),
    ]

    mismatches = []
    for vw, vh, label in sizes:
        _load_corner_page(server_entry, vw, vh)
        vp = _get_viewport(server_entry)
        actual_vw = int(vp.get("width") or 0)
        actual_vh = int(vp.get("height") or 0)

        orig = DI.agent_factory.create_agent
        DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "top left")
        try:
            res = _run_web_page_coords("Find the Top Left Button")
        finally:
            DI.agent_factory.create_agent = orig

        image_path = res.data.get("image_path") or ""
        if not image_path or not Path(image_path).exists():
            mismatches.append(f"{label}: no screenshot saved")
            continue

        png_w, png_h = _get_png_dimensions(image_path)
        match = (png_w == actual_vw and png_h == actual_vh)
        status = "MATCH" if match else f"MISMATCH (PNG={png_w}x{png_h} vs viewport={actual_vw}x{actual_vh})"
        print(f"\n[Q3 {label}] {status}  screenshot: {image_path}")
        if not match:
            mismatches.append(
                f"{label}: PNG={png_w}x{png_h} != viewport={actual_vw}x{actual_vh} — {image_path}"
            )

    assert not mismatches, (
        "Screenshot dimensions do not match viewport at:\n"
        + "\n".join(f"  {m}" for m in mismatches)
        + "\n\nThis means the vision model sees the screenshot at a different scale "
        "than the DOM coordinate system — badge pixel positions in the image do not "
        "correspond to the (x,y) values stored in window.__emi_marks_map."
    )


# ---------------------------------------------------------------------------
# Regression: stale marks when viewport changes between inject and screenshot
# ---------------------------------------------------------------------------

def test_stale_marks_produce_wrong_coords_resize_between_inject_and_click():
    """
    Demonstrates the failure mode when marks are injected BEFORE a resize,
    then web_page_coords is called AFTER. This is the scenario that produces
    the reported inaccurate coords near the viewport edge.

    Concretely:
      - Marks injected at 900x600 → BR stored at (~830, ~568)
      - Viewport resized to 2560x1400
      - web_page_coords called fresh → injects NEW marks at (~2490, ~1368)
      - The fresh pipeline is correct; stale marks (from manual inject) are wrong.

    We verify that calling web_page_coords fresh AFTER resize gives correct
    results, and that the old stale coords would have been wrong.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    small_w, small_h = 900, 600
    large_w, large_h = 2560, 1400

    # Step 1: load at small viewport, manually inject marks (simulating a prior call).
    _load_corner_page(server_entry, small_w, small_h)
    from app.assistant.lib.tools.web_page_coords.web_page_coords import WebPageCoords
    WebPageCoords()._inject_marks(
        server_entry=server_entry, question="Bottom Right Button", max_marks=25
    )
    stale_marks = _get_marks_map(server_entry)
    br_stale = next(
        (m for m in stale_marks if "bottom right" in str(m.get("label") or "").lower()), None
    )
    assert br_stale, "BR not in stale marks"
    stale_x, stale_y = float(br_stale["x"]), float(br_stale["y"])
    assert stale_x < small_w and stale_y < small_h, "Stale coords not from small viewport"

    # Step 2: resize to large viewport — stale marks are now wrong.
    js_resize = f"""
async (page) => {{
  await page.setViewportSize({{ width: {large_w}, height: {large_h} }});
  return {{ ok: true }};
}}
"""
    _mcp_run(server_entry, js_resize, timeout_s=10)

    # The stale coords should land far from the real BR button position at the new size.
    expected_br_x_large = large_w - 70
    expected_br_y_large = large_h - 32
    assert abs(stale_x - expected_br_x_large) > 100 or abs(stale_y - expected_br_y_large) > 100, (
        "Stale coords are unexpectedly close to large-viewport BR position — test setup issue."
    )

    # Step 3: call web_page_coords fresh — it should reinject marks at the new size.
    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "bottom right")
    try:
        res = _run_web_page_coords("Find the Bottom Right Button")
    finally:
        DI.agent_factory.create_agent = orig

    targets = res.data.get("targets") or []
    assert targets, "No targets from fresh web_page_coords after resize"
    fresh_x, fresh_y = float(targets[0]["x"]), float(targets[0]["y"])

    assert abs(fresh_x - expected_br_x_large) <= 40, (
        f"Fresh coords x={fresh_x} not near {expected_br_x_large} after resize.\n"
        f"web_page_coords did not reinject marks correctly."
    )
    assert abs(fresh_y - expected_br_y_large) <= 40, (
        f"Fresh coords y={fresh_y} not near {expected_br_y_large} after resize."
    )

    # Click and verify.
    _remove_marks_overlay(server_entry)
    _click_xy(server_entry, fresh_x, fresh_y)
    result = _get_click_result(server_entry)
    assert result == "clicked:btn-br", (
        f"Fresh coords ({fresh_x},{fresh_y}) at large viewport clicked '{result}' not btn-br.\n"
        f"Stale would have clicked near ({stale_x},{stale_y}) — wrong position."
    )

    print(
        f"\n[STALE REGRESSION] stale=({stale_x},{stale_y}), "
        f"fresh=({fresh_x},{fresh_y}), result='{result}'"
    )


# ---------------------------------------------------------------------------
# Q4: Returned coords are element centers, not badge positions.
# ---------------------------------------------------------------------------

def test_returned_coords_are_element_center_not_badge_position():
    """
    Contract test: web_page_coords must return the CENTER of the target element,
    not the position of the numbered badge (which is drawn at the element's
    top-left corner, outside the element, at approximately (rect.left-14, rect.top-14)).

    Verification:
      1. Run web_page_coords for a known button.
      2. Fetch the button's bounding rect directly from the DOM.
      3. Assert that the returned (x, y) equals (rect.left + width/2, rect.top + height/2).
      4. Assert that (x, y) is NOT near the badge position (rect.left-14, rect.top-14).
      5. Assert that elementFromPoint(x, y) hits the correct button.

    This is important because the badge sits outside the element — clicking the
    badge position would miss the button entirely.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    _load_corner_page(server_entry, 1280, 800)

    orig = DI.agent_factory.create_agent
    DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "bottom right")
    try:
        res = _run_web_page_coords("Find the Bottom Right Button")
    finally:
        DI.agent_factory.create_agent = orig

    assert res.data.get("marked") is True, "No marks produced"
    targets = res.data.get("targets") or []
    assert targets, "No targets returned"
    x, y = float(targets[0]["x"]), float(targets[0]["y"])

    # Fetch the actual DOM bounding rect of btn-br.
    rect_raw = _mcp_run(server_entry, """
async (page) => {
  return await page.evaluate(() => {
    const el = document.getElementById('btn-br');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  });
}
""")
    assert isinstance(rect_raw, dict), f"Could not get bounding rect: {rect_raw!r}"
    left = float(rect_raw["left"])
    top = float(rect_raw["top"])
    width = float(rect_raw["width"])
    height = float(rect_raw["height"])

    expected_center_x = left + width / 2
    expected_center_y = top + height / 2
    badge_x = max(left - 14, 0)   # badge is drawn at rect.left-14 (clamped to 0)
    badge_y = max(top - 14, 0)    # badge is drawn at rect.top-14  (clamped to 0)

    # Coord must be close to element center.
    assert abs(x - expected_center_x) <= 4, (
        f"Returned x={x} is not element center {expected_center_x:.1f} "
        f"(rect: left={left}, width={width}).\n"
        f"If x were near badge_x={badge_x:.1f}, the click would miss the button."
    )
    assert abs(y - expected_center_y) <= 4, (
        f"Returned y={y} is not element center {expected_center_y:.1f} "
        f"(rect: top={top}, height={height}).\n"
        f"If y were near badge_y={badge_y:.1f}, the click would miss the button."
    )

    # Coord must NOT be at the badge position.
    at_badge_x = abs(x - badge_x) < 10
    at_badge_y = abs(y - badge_y) < 10
    assert not (at_badge_x and at_badge_y), (
        f"Returned coords ({x},{y}) are at the BADGE position ({badge_x:.1f},{badge_y:.1f}), "
        f"not the element center ({expected_center_x:.1f},{expected_center_y:.1f}).\n"
        f"Clicking the badge position will miss the button."
    )

    # elementFromPoint must hit the correct button.
    info = _element_at(server_entry, x, y)
    assert info.get("found"), f"elementFromPoint({x},{y}) found nothing"
    assert (
        info.get("id") == "btn-br"
        or "bottom right" in str(info.get("aria") or "").lower()
    ), (
        f"Coords ({x},{y}) hit '{info.get('id')}' not btn-br.\n"
        f"Element center was ({expected_center_x:.1f},{expected_center_y:.1f}), "
        f"badge was at ({badge_x:.1f},{badge_y:.1f})."
    )

    print(
        f"\n[Q4] btn-br: rect=({left:.0f},{top:.0f} {width:.0f}x{height:.0f}), "
        f"center=({expected_center_x:.1f},{expected_center_y:.1f}), "
        f"returned=({x},{y}), badge=({badge_x:.1f},{badge_y:.1f})"
    )


def test_returned_coords_are_element_center_after_browser_resize():
    """
    Same center-vs-badge contract after browser_resize.

    After resize, element positions shift (especially for fixed-position elements
    near the far edges). Verify that web_page_coords still returns the element
    center, not the badge position, for both a near-edge (BR) and near-origin (TL)
    button.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    _load_corner_page(server_entry, 1280, 800)
    _browser_resize(server_entry, width=2048, height=1200)

    vp = _get_viewport(server_entry)
    vp_w = int(vp.get("width") or 0)
    vp_h = int(vp.get("height") or 0)
    assert vp_w >= 1400, f"browser_resize did not take effect: {vp}"

    for btn_id, target_substr in [("btn-br", "bottom right"), ("btn-tl", "top left")]:
        orig = DI.agent_factory.create_agent
        DI.agent_factory.create_agent = _patch_vision(orig, server_entry, target_substr)
        try:
            res = _run_web_page_coords(f"Find the {target_substr.title()} Button")
        finally:
            DI.agent_factory.create_agent = orig

        targets = res.data.get("targets") or []
        assert targets, f"No targets for {btn_id} after browser_resize"
        x, y = float(targets[0]["x"]), float(targets[0]["y"])

        rect_js = f"""
async (page) => {{
  return await page.evaluate(() => {{
    const el = document.getElementById({json.dumps(btn_id)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {{ left: r.left, top: r.top, width: r.width, height: r.height }};
  }});
}}
"""
        rect_raw = _mcp_run(server_entry, rect_js)
        assert isinstance(rect_raw, dict), f"No rect for {btn_id}: {rect_raw!r}"
        left = float(rect_raw["left"])
        top_ = float(rect_raw["top"])
        width = float(rect_raw["width"])
        height = float(rect_raw["height"])

        expected_cx = left + width / 2
        expected_cy = top_ + height / 2
        badge_x = max(left - 14, 0)
        badge_y = max(top_ - 14, 0)

        assert abs(x - expected_cx) <= 4, (
            f"{btn_id} after resize({vp_w}x{vp_h}): "
            f"x={x} not near center {expected_cx:.1f} (badge would be at {badge_x:.1f})"
        )
        assert abs(y - expected_cy) <= 4, (
            f"{btn_id} after resize({vp_w}x{vp_h}): "
            f"y={y} not near center {expected_cy:.1f} (badge would be at {badge_y:.1f})"
        )

        info = _element_at(server_entry, x, y)
        assert info.get("found") and info.get("id") == btn_id, (
            f"{btn_id} after resize: elementFromPoint({x},{y}) hit {info!r}, not {btn_id}.\n"
            f"center=({expected_cx:.1f},{expected_cy:.1f}), badge=({badge_x:.1f},{badge_y:.1f})"
        )

        print(
            f"\n[Q4 resize] {btn_id}: center=({expected_cx:.1f},{expected_cy:.1f}), "
            f"returned=({x},{y}), badge=({badge_x:.1f},{badge_y:.1f}) — OK"
        )


# ---------------------------------------------------------------------------
# Q5: Badge legibility across zoom levels.
#
# When browser_resize sets a viewport larger than the physical display,
# browser_take_screenshot captures at native display resolution. This shrinks
# every CSS pixel to fewer PNG pixels. The badge font is fixed at 14px CSS,
# so it becomes 14 * (png_width / css_width) pixels tall in the screenshot.
#
# The question: at each zoom level, how large is a badge in the PNG?  Is it
# still large enough for a vision model to read the digit, and is spacing
# between adjacent badges still distinguishable?
#
# We measure:
#   - scale_factor       = png_width / css_viewport_width
#   - badge_px           = floor(14 * scale_factor)   (font height in PNG pixels)
#   - min_badge_sep_px   = min pairwise distance between adjacent badge centers
#                          in PNG coords
#   - overlap_count      = badges whose PNG bounding boxes overlap another badge
#
# Hard threshold: badge_px < 6 means the digit is likely unreadable to an LLM.
# Soft threshold: badge_px < 9 is a warning (may degrade accuracy for small
# badges when many marks are clustered).
# ---------------------------------------------------------------------------

# Dense UI page: a grid of small buttons that stresses badge separation.
_DENSE_GRID_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"/><title>Dense Grid</title>'
    '<style>'
    'body{margin:8px;background:#f1f5f9;font-family:Arial,sans-serif;}'
    '.grid{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;}'
    '.btn{padding:8px 4px;border:1px solid #94a3b8;border-radius:6px;'
    'background:#fff;font-size:11px;cursor:pointer;text-align:center;}'
    '</style></head><body><div class="grid">'
    + "".join(
        f'<button class="btn" id="item-{i}" aria-label="Item {i}">Item {i}</button>'
        for i in range(1, 25)
    )
    + '</div></body></html>'
)


def _badge_legibility_stats(server_entry: dict, image_path: str, css_vw: int, png_w: int) -> dict:
    """
    Compute badge legibility statistics for a screenshot.

    Returns a dict with:
      scale_factor     : png_w / css_vw
      badge_css_px     : badge font size in CSS pixels (always 14)
      badge_png_px     : badge_css_px * scale_factor (rendered size in PNG)
      marks_count      : number of marks in the map
      min_sep_png_px   : minimum center-to-center distance between any two badges
                         in PNG pixel coords (None if fewer than 2 marks)
      overlap_count    : number of badge pairs whose PNG bboxes overlap
                         (assuming each badge is ~badge_png_px * 2 wide, * 1.5 tall)
    """
    marks = _get_marks_map(server_entry)
    scale = png_w / css_vw if css_vw else 1.0
    badge_css = 14.0
    badge_png = badge_css * scale

    # Badge dimensions in PNG pixels (approximate rendered size of the label div).
    # CSS: padding 1px 5px, font 14px/1.1 → ~16px tall, ~22-28px wide for 1-2 digit number.
    badge_h_png = (badge_css * 1.1 + 2) * scale   # line-height + padding
    badge_w_png = 28 * scale                        # rough width for 2-digit badge

    # Convert badge CSS positions to PNG positions.
    badge_positions = []
    for m in marks:
        rect = m.get("rect") or {}
        l = float(rect.get("l") or m.get("x") or 0)
        t = float(rect.get("t") or m.get("y") or 0)
        # Badge sits at (max(l-14,0), max(t-14,0)) in CSS pixels.
        bx_css = max(l - 14, 0)
        by_css = max(t - 14, 0)
        bx_png = bx_css * scale
        by_png = by_css * scale
        badge_positions.append({
            "id": m.get("id"),
            "cx_png": bx_png + badge_w_png / 2,
            "cy_png": by_png + badge_h_png / 2,
            "l_png": bx_png,
            "t_png": by_png,
            "r_png": bx_png + badge_w_png,
            "b_png": by_png + badge_h_png,
        })

    min_sep = None
    overlap_count = 0
    for i, a in enumerate(badge_positions):
        for b in badge_positions[i + 1:]:
            dist = ((a["cx_png"] - b["cx_png"]) ** 2 + (a["cy_png"] - b["cy_png"]) ** 2) ** 0.5
            if min_sep is None or dist < min_sep:
                min_sep = dist
            # Check bounding box overlap.
            x_overlap = a["l_png"] < b["r_png"] and a["r_png"] > b["l_png"]
            y_overlap = a["t_png"] < b["b_png"] and a["b_png"] > b["t_png"]
            if x_overlap and y_overlap:
                overlap_count += 1

    return {
        "scale_factor": round(scale, 3),
        "badge_css_px": badge_css,
        "badge_png_px": round(badge_png, 1),
        "marks_count": len(marks),
        "min_sep_png_px": round(min_sep, 1) if min_sep is not None else None,
        "overlap_count": overlap_count,
    }


def _load_dense_page(server_entry: dict, vw: int, vh: int) -> None:
    js = f"""
async (page) => {{
  await page.setViewportSize({{ width: {vw}, height: {vh} }});
  await page.setContent({json.dumps(_DENSE_GRID_HTML)}, {{ waitUntil: 'domcontentloaded' }});
  return {{ ok: true }};
}}
"""
    _mcp_run(server_entry, js, timeout_s=30)


def test_q5_badge_legibility_across_zoom_levels():
    """
    Q5: Can the vision model still read badges when browser_resize zooms out?

    For each viewport size we:
      1. Load a dense 24-button grid (stresses badge spacing).
      2. Run web_page_coords (full pipeline with stub vision).
      3. Capture the screenshot.
      4. Compute badge_png_px = badge_css_px * (png_width / css_viewport_width).
      5. Check whether badges overlap in the PNG.
      6. Print a legibility assessment.

    Hard fail: badge_png_px < 6 — digit is too small to read reliably.
    Warning (printed, not a hard fail): badge_png_px < 9.
    Hard fail: any two badges overlap in PNG coords — vision model cannot tell
               which badge belongs to which element.

    The test prints a full report so you can see exactly where the threshold is
    on this machine's display configuration.
    """
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict)

    # MINIMUM_BADGE_PX: below this, a 14px bold digit becomes unreadable in the PNG.
    # GPT-4o vision can typically read down to ~7-8px rendered font height.
    MINIMUM_BADGE_PX = 6.0

    report_lines = []
    hard_failures = []

    sizes = [
        (1280,  800,  "native 1280×800  (baseline)"),
        (1600, 1000,  "1600×1000        (+25% width)"),
        (1920, 1080,  "1920×1080        (HD)"),
        (2560, 1440,  "2560×1440        (2× wide)"),
        (3840, 2160,  "3840×2160        (4K — extreme)"),
    ]

    for css_vw, css_vh, label in sizes:
        _load_dense_page(server_entry, css_vw, css_vh)

        orig = DI.agent_factory.create_agent
        DI.agent_factory.create_agent = _patch_vision(orig, server_entry, "item 1")
        try:
            res = _run_web_page_coords("Find Item 1")
        finally:
            DI.agent_factory.create_agent = orig

        image_path = res.data.get("image_path") or ""
        if not image_path or not Path(image_path).exists():
            hard_failures.append(f"{label}: no screenshot saved (marked={res.data.get('marked')!r}, {res.content[:80]!r})")
            continue

        png_w, png_h = _get_png_dimensions(image_path)
        stats = _badge_legibility_stats(server_entry, image_path, css_vw, png_w)

        scale = stats["scale_factor"]
        badge_px = stats["badge_png_px"]
        min_sep = stats["min_sep_png_px"]
        overlaps = stats["overlap_count"]
        n_marks = stats["marks_count"]

        if badge_px < MINIMUM_BADGE_PX:
            verdict = f"❌ UNREADABLE  badge={badge_px}px < {MINIMUM_BADGE_PX}px minimum"
            hard_failures.append(f"{label}: {verdict}")
        elif badge_px < 9.0:
            verdict = f"⚠️  MARGINAL    badge={badge_px}px (< 9px soft threshold)"
        else:
            verdict = f"✅ LEGIBLE     badge={badge_px}px"

        sep_note = f"min_sep={min_sep}px" if min_sep is not None else "n/a"
        overlap_note = f"overlaps={overlaps}" if overlaps else "no overlaps"

        if overlaps > 0:
            overlap_msg = f"❌ {overlaps} badge pair(s) overlap in PNG"
            hard_failures.append(f"{label}: {overlap_msg}")

        line = (
            f"  {label}\n"
            f"    CSS={css_vw}×{css_vh}  PNG={png_w}×{png_h}  "
            f"scale={scale:.3f}  marks={n_marks}\n"
            f"    {verdict}\n"
            f"    {sep_note}  {overlap_note}"
        )
        report_lines.append(line)

    print("\n\n=== Q5 Badge Legibility Report ===")
    print("\n".join(report_lines))
    if hard_failures:
        print("\nFAILURES:")
        for f in hard_failures:
            print(f"  {f}")
    else:
        print("\nAll sizes pass legibility checks.")
    print("=" * 40)

    assert not hard_failures, (
        "Badge legibility failures detected — vision model will likely misread badges:\n"
        + "\n".join(f"  {f}" for f in hard_failures)
        + "\n\nThis means browser_resize beyond your display's native resolution "
        "produces screenshots where badge numbers are too small or overlapping "
        "for reliable vision model interpretation."
    )


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_q1_browser_resize_reveals_below_fold_button,
        test_q2_web_page_coords_click_accuracy_before_browser_resize,
        test_q2_web_page_coords_click_accuracy_after_browser_resize,
        test_q2_top_left_button_click_accurate_before_and_after_browser_resize,
        test_q3_screenshot_dimensions_match_viewport_at_default_size,
        test_q3_screenshot_dimensions_match_viewport_after_browser_resize,
        test_q3_screenshot_dimensions_at_multiple_viewport_sizes,
        test_stale_marks_produce_wrong_coords_resize_between_inject_and_click,
        test_returned_coords_are_element_center_not_badge_position,
        test_returned_coords_are_element_center_after_browser_resize,
        test_q5_badge_legibility_across_zoom_levels,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
        except Exception as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n{'All passed' if not failed else f'{failed} failed'} ({len(tests)} total)")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
