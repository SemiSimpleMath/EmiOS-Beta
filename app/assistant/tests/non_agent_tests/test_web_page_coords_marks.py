import json
import re
import subprocess
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `import app...` works when running by file path.
_HERE = Path(__file__).resolve()
for parent in _HERE.parents:
    if (parent / "app").is_dir():
        sys.path.insert(0, str(parent))
        break

import app.assistant.tests.test_setup  # noqa: F401 (side-effect: initializes DI)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


VIEWPORT_WIDTH = 2048
VIEWPORT_HEIGHT = 1400


def _parse_jsonish(text: str):
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        # Many MCP tools return "### Result" + JSON + extra echo blocks.
        try:
            obj, _idx = json.JSONDecoder().raw_decode(s.lstrip())
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
                obj, _idx = json.JSONDecoder().raw_decode(payload.lstrip())
                return obj
            except Exception:
                # If it's not JSON (often it's a code echo), keep searching.
                pass

    # Last resort: parse JSON prefix starting from first '{' or '['
    i1 = min([i for i in [s.find("["), s.find("{")] if i >= 0] or [-1])
    if i1 >= 0:
        tail = s[i1:]
        try:
            obj, _idx = json.JSONDecoder().raw_decode(tail.lstrip())
            return obj
        except Exception:
            return None
    return None


def _refresh_playwright_mcp_tool_cache(repo_root: Path) -> None:
    script = repo_root / "mcp" / "refresh_tool_cache.py"
    cmd = [
        sys.executable,
        str(script),
        "--server-id",
        "npm/playwright-mcp",
        "--launch-id",
        "cmd_npx",
        "--timeout",
        "60",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to refresh Playwright MCP tool cache.\n"
            + f"exit_code={proc.returncode}\n"
            + (f"\n[stdout]\n{proc.stdout}" if proc.stdout else "")
            + (f"\n[stderr]\n{proc.stderr}" if proc.stderr else "")
        )


def _ensure_playwright_mcp_tools() -> None:
    required = [
        "mcp::npm/playwright-mcp::browser_run_code",
        "mcp::npm/playwright-mcp::browser_take_screenshot",
        "mcp::npm/playwright-mcp::browser_mouse_click_xy",
    ]
    missing = [t for t in required if not DI.tool_registry.get_tool(t)]
    if not missing:
        return
    repo_root = Path(__file__).resolve().parents[3]
    _refresh_playwright_mcp_tool_cache(repo_root)
    DI.tool_registry.load_mcp_tool_cache(enabled_only=True)
    missing2 = [t for t in required if not DI.tool_registry.get_tool(t)]
    if missing2:
        raise RuntimeError("Missing Playwright MCP tools even after refresh:\n" + "\n".join(f"- {t}" for t in missing2))


def _set_busy_test_page(server_entry: dict) -> None:
    html = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Busy Test Page</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; }
    header { background: #111827; color: #fff; padding: 14px 18px; }
    .grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; padding: 16px; }
    .btn { padding: 10px 8px; border-radius: 10px; border: 1px solid #cbd5e1; background: #f8fafc; cursor: pointer; }
    .btn:hover { background: #e2e8f0; }
    #addresses-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: flex; align-items: center; justify-content: center; }
    #modal-card { width: 520px; background: white; border-radius: 14px; box-shadow: 0 10px 35px rgba(0,0,0,0.35); padding: 18px 18px 14px; position: relative; }
    #modal-title { font-size: 18px; font-weight: 700; margin: 0 0 8px; }
    #modal-close { position: absolute; top: 12px; right: 12px; width: 34px; height: 34px; border-radius: 10px; border: 1px solid #e2e8f0; background: #fff; cursor: pointer; }
    #modal-close:hover { background: #f1f5f9; }
    .row { display: flex; gap: 10px; margin-top: 10px; }
    .pill { padding: 10px 12px; border-radius: 999px; border: 1px solid #e2e8f0; background: #f8fafc; }
  </style>
</head>
<body>
  <header>Busy Page Header — lots of clickable stuff</header>
  <div style="padding: 12px 16px; background: #f1f5f9; border-bottom: 1px solid #e2e8f0;">
    <label for="item-search" style="display:block; font-weight:700; margin-bottom:6px;">Item Search</label>
    <input id="item-search" aria-label="Item Search" placeholder="Search items..." style="width: 420px; padding: 10px 12px; border-radius: 12px; border: 1px solid #cbd5e1;" />
  </div>
  <main class="grid" id="main-grid"></main>

  <div id="addresses-modal" role="dialog" aria-label="Addresses modal">
    <div id="modal-card">
      <button id="modal-close" aria-label="Close Addresses modal">×</button>
      <div id="modal-title">Addresses</div>
      <div style="margin-top: 10px;">
        <label for="modal-address-search" style="display:block; font-weight:700; margin-bottom:6px;">Address</label>
        <input id="modal-address-search" aria-label="Address input" placeholder="Enter address..." style="width: 460px; padding: 10px 12px; border-radius: 12px; border: 1px solid #cbd5e1;" />
      </div>
      <div class="row">
        <div class="pill">123 Main St, Springfield, CA 92614</div>
        <button class="btn" aria-label="Confirm address">Confirm</button>
      </div>
      <div class="row">
        <button class="btn" aria-label="Add new address">Add new</button>
        <button class="btn" aria-label="Use current location">Use current location</button>
      </div>
    </div>
  </div>

  <script>
    const grid = document.getElementById('main-grid');
    for (let i = 1; i <= 36; i++) {
      const b = document.createElement('button');
      b.className = 'btn';
      b.textContent = 'Button ' + i;
      b.setAttribute('aria-label', 'Background Button ' + i);
      b.addEventListener('click', () => console.log('clicked background', i));
      grid.appendChild(b);
    }
    document.getElementById('modal-close').addEventListener('click', () => {
      const m = document.getElementById('addresses-modal');
      if (m) m.remove();
    });
  </script>
</body>
</html>
"""

    js = f"""
async (page) => {{
  await page.setViewportSize({{ width: {int(VIEWPORT_WIDTH)}, height: {int(VIEWPORT_HEIGHT)} }});
  await page.setContent({json.dumps(html)}, {{ waitUntil: 'domcontentloaded' }});
  return {{ ok: true }};
}}
"""
    _ = mcp_stdio_call_tool(server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js}, timeout_s=30)


def _set_hover_reveal_test_page(server_entry: dict) -> None:
    html = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Hover Reveal Test</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; padding: 20px; }
    #card {
      width: 720px;
      height: 240px;
      border-radius: 16px;
      border: 1px solid #cbd5e1;
      background: linear-gradient(135deg, #0ea5e9, #22c55e);
      position: relative;
      overflow: hidden;
      cursor: default;
    }
    #card .title { position: absolute; left: 18px; bottom: 16px; color: white; font-weight: 700; font-size: 20px; }
    /* Heart button always visible (false-positive bait) */
    #heart {
      position: absolute; top: 12px; right: 12px;
      width: 40px; height: 40px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.55);
      background: rgba(255,255,255,0.25);
      color: white;
      cursor: pointer;
      z-index: 5;
    }
    /* Real clickable overlay only appears on hover */
    #open-link {
      position: absolute; inset: 0;
      display: none;
      cursor: pointer;
      z-index: 2;
    }
    #card:hover #open-link { display: block; }
    #open-link span {
      position: absolute; left: 18px; top: 16px;
      background: rgba(0,0,0,0.45);
      color: white;
      padding: 8px 10px;
      border-radius: 999px;
      font-weight: 700;
    }
  </style>
</head>
<body>
  <div id="card">
    <button id="heart" aria-label="Add this store to your saved list">♥</button>
    <a id="open-link" href="/store/123" aria-label="Open restaurant tile">
      <span>Open store</span>
    </a>
    <div class="title">The Cut Handcrafted Burgers</div>
  </div>
</body>
</html>
"""

    js = f"""
async (page) => {{
  await page.setViewportSize({{ width: {int(VIEWPORT_WIDTH)}, height: {int(VIEWPORT_HEIGHT)} }});
  await page.setContent({json.dumps(html)}, {{ waitUntil: 'domcontentloaded' }});
  return {{ ok: true }};
}}
"""
    _ = mcp_stdio_call_tool(server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js}, timeout_s=30)


class _StubVisionMarkPicker:
    """
    Deterministic stub: always pick mark_id 1.
    Our busy test page is designed so the modal close button is highest-scored -> mark id 1.
    """

    def action_handler(self, _msg):
        return ToolResult(
            result_type="llm_result",
            content="stub mark picker",
            data={"action": "done", "mark_ids": [1], "confidence": 1.0, "rationale": "always pick mark 1"},
        )


def test_web_page_coords_marks_returns_clickable_coords_for_modal_close():
    _ensure_playwright_mcp_tools()

    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"

    _set_busy_test_page(server_entry)

    # Patch agent factory so this test does not depend on OpenAI.
    orig_create_agent = DI.agent_factory.create_agent

    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_mark_picker":
            return _StubVisionMarkPicker()
        return orig_create_agent(name, blackboard=blackboard)

    DI.agent_factory.create_agent = _create_agent
    try:
        tool_cfg = DI.tool_registry.get_tool("web_page_coords")
        assert tool_cfg and tool_cfg.get("tool_class"), "web_page_coords tool not registered"
        tool = tool_cfg["tool_class"]()

        res = tool.execute(
            ToolMessage(
                tool_name="web_page_coords",
                tool_data={
                    "tool_name": "web_page_coords",
                    "arguments": {
                        "question": "Find the close (X) button on the open Addresses modal and return its coordinates",
                        
                        "strict": True,
                    },
                },
            )
        )
        assert res.result_type == "web_page_coords"
        assert isinstance(res.data, dict)
        assert res.data.get("marked") is True
        targets = res.data.get("targets")
        assert isinstance(targets, list) and targets, "Expected at least 1 target"
        t0 = targets[0]
        assert isinstance(t0, dict)
        x = float(t0["x"])
        y = float(t0["y"])

        # Verify the returned coords point at the actual close button.
        js_check = f"""
async (page) => {{
  const x = {x};
  const y = {y};
  return await page.evaluate(({{x, y}}) => {{
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    return {{
      tag: el.tagName,
      id: el.id || null,
      aria: el.getAttribute('aria-label') || null,
      text: (el.innerText || el.textContent || '').trim().slice(0, 80),
    }};
  }}, {{x, y}});
}}
"""
        resp = mcp_stdio_call_tool(
            server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_check}, timeout_s=20
        )
        # The MCP response text is embedded inside the tool response; parse best-effort.
        text_items = ((resp or {}).get("result") or {}).get("content") or []
        text = ""
        for it in text_items:
            if isinstance(it, dict) and it.get("type") == "text":
                text = it.get("text") or ""
                break
        info = _parse_jsonish(text)
        assert isinstance(info, dict), f"Could not parse elementFromPoint JSON. raw={text!r}"
        assert info.get("id") == "modal-close" or "close" in str(info.get("aria") or "").lower()

        # Click and verify the modal disappears.
        _ = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name="browser_mouse_click_xy",
            arguments={"x": x, "y": y},
            timeout_s=20,
        )
        js_modal = """
async (page) => {
  return await page.evaluate(() => {
    return { present: Boolean(document.getElementById('addresses-modal')) };
  });
}
"""
        resp2 = mcp_stdio_call_tool(
            server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_modal}, timeout_s=20
        )
        text_items2 = ((resp2 or {}).get("result") or {}).get("content") or []
        text2 = ""
        for it in text_items2:
            if isinstance(it, dict) and it.get("type") == "text":
                text2 = it.get("text") or ""
                break
        present = _parse_jsonish(text2)
        assert isinstance(present, dict) and present.get("present") is False
    finally:
        DI.agent_factory.create_agent = orig_create_agent


def test_web_page_coords_hover_probe_prefers_large_hover_revealed_tile_over_heart():
    _ensure_playwright_mcp_tools()

    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"

    _set_hover_reveal_test_page(server_entry)

    # Patch agent factory so this test does not depend on OpenAI.
    orig_create_agent = DI.agent_factory.create_agent

    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_mark_picker":
            return _StubVisionMarkPicker()
        return orig_create_agent(name, blackboard=blackboard)

    DI.agent_factory.create_agent = _create_agent
    try:
        tool_cfg = DI.tool_registry.get_tool("web_page_coords")
        assert tool_cfg and tool_cfg.get("tool_class"), "web_page_coords tool not registered"
        tool = tool_cfg["tool_class"]()

        res = tool.execute(
            ToolMessage(
                tool_name="web_page_coords",
                tool_data={
                    "tool_name": "web_page_coords",
                    "arguments": {
                        "question": "Open the restaurant tile (not the heart)",
                        
                        "strict": True,
                    },
                },
            )
        )
        assert res.result_type == "web_page_coords"
        assert isinstance(res.data, dict)
        targets = res.data.get("targets")
        assert isinstance(targets, list) and targets, "Expected at least 1 target"
        t0 = targets[0]
        assert isinstance(t0, dict)
        x = float(t0["x"])
        y = float(t0["y"])

        # Verify returned coords point at the overlay link (A#open-link), not the heart button.
        #
        # Important: the real link only appears on hover. So we must hover at the returned coords
        # before using elementFromPoint.
        js_check = f"""
async (page) => {{
  const x = {x};
  const y = {y};
  await page.mouse.move(x, y);
  await page.waitForTimeout(80);
  return await page.evaluate(({{x, y}}) => {{
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const a = el.closest ? el.closest('a') : null;
    return {{
      tag: el.tagName,
      id: el.id || null,
      aria: el.getAttribute('aria-label') || null,
      href: a ? (a.getAttribute('href') || null) : null,
    }};
  }}, {{x, y}});
}}
"""
        resp = mcp_stdio_call_tool(
            server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_check}, timeout_s=20
        )
        text_items = ((resp or {}).get("result") or {}).get("content") or []
        text = ""
        for it in text_items:
            if isinstance(it, dict) and it.get("type") == "text":
                text = it.get("text") or ""
                break
        info = _parse_jsonish(text)
        assert isinstance(info, dict), f"Could not parse elementFromPoint JSON. raw={text!r}"
        assert str(info.get("href") or "").startswith("/store/"), f"Expected /store link, got {info!r}"
        assert info.get("id") != "heart", f"Clicked heart instead of tile: {info!r}"
    finally:
        DI.agent_factory.create_agent = orig_create_agent


def test_web_page_coords_marks_map_contains_rects_centered_within_10px():
    """
    Verify our "red box + center tag" overlay data is present and consistent.

    We don't inspect pixels; we validate the JS marks map contains `rect` and that each mark's (x,y)
    is near the rect center (±10px).
    """
    _ensure_playwright_mcp_tools()

    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"

    _set_busy_test_page(server_entry)

    # Patch agent factory so this test does not depend on OpenAI.
    orig_create_agent = DI.agent_factory.create_agent

    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_mark_picker":
            return _StubVisionMarkPicker()
        return orig_create_agent(name, blackboard=blackboard)

    DI.agent_factory.create_agent = _create_agent
    try:
        tool_cfg = DI.tool_registry.get_tool("web_page_coords")
        assert tool_cfg and tool_cfg.get("tool_class"), "web_page_coords tool not registered"
        tool = tool_cfg["tool_class"]()

        res = tool.execute(
            ToolMessage(
                tool_name="web_page_coords",
                tool_data={
                    "tool_name": "web_page_coords",
                    "arguments": {
                        "question": "Find the close (X) button on the open Addresses modal and return its coordinates",
                        
                        "strict": True,
                    },
                },
            )
        )
        assert res.result_type == "web_page_coords"
        assert isinstance(res.data, dict)
        assert res.data.get("marked") is True
        assert int(res.data.get("marks_count") or 0) >= 1

        # Fetch the persisted marks map from the page (we keep it even after overlay removal).
        js_map = """
async (page) => {
  return await page.evaluate(() => {
    const mm = (window.__emi_marks_map && Array.isArray(window.__emi_marks_map)) ? window.__emi_marks_map : [];
    return mm.slice(0, 8);
  });
}
""".strip()
        resp = mcp_stdio_call_tool(
            server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_map}, timeout_s=20
        )
        text_items = ((resp or {}).get("result") or {}).get("content") or []
        text = ""
        for it in text_items:
            if isinstance(it, dict) and it.get("type") == "text":
                text = it.get("text") or ""
                break
        mm = _parse_jsonish(text)
        assert isinstance(mm, list) and mm, f"Expected non-empty __emi_marks_map slice. raw={text!r}"

        for m in mm:
            assert isinstance(m, dict)
            assert isinstance(m.get("id"), int)
            assert isinstance(m.get("x"), (int, float))
            assert isinstance(m.get("y"), (int, float))
            rect = m.get("rect")
            assert isinstance(rect, dict), f"Missing rect on mark: {m!r}"
            for k in ("l", "t", "w", "h"):
                assert isinstance(rect.get(k), (int, float)), f"rect.{k} missing/invalid: {rect!r}"

            cx = float(rect["l"]) + float(rect["w"]) / 2.0
            cy = float(rect["t"]) + float(rect["h"]) / 2.0
            dx = abs(float(m["x"]) - cx)
            dy = abs(float(m["y"]) - cy)
            assert dx <= 10.0 and dy <= 10.0, f"Mark not centered within 10px: dx={dx} dy={dy} mark={m!r}"
    finally:
        DI.agent_factory.create_agent = orig_create_agent


def test_web_page_coords_includes_textbox_mark_and_center_hits_input():
    """
    Ensure textbox candidates are included in marks and that at least one mark center
    lands on the Item Search <input> we embed in the busy test page.
    """
    _ensure_playwright_mcp_tools()

    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"

    _set_busy_test_page(server_entry)

    # Remove the modal overlay so the textbox is actually hittable by elementFromPoint.
    # (Other tests keep the modal to validate modal-close targeting.)
    js_remove_modal = """
async (page) => {
  return await page.evaluate(() => {
    const m = document.getElementById('addresses-modal');
    if (m) m.remove();
    return { ok: true };
  });
}
""".strip()
    _ = mcp_stdio_call_tool(
        server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_remove_modal}, timeout_s=20
    )

    # Patch agent factory so this test does not depend on OpenAI.
    orig_create_agent = DI.agent_factory.create_agent

    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_mark_picker":
            return _StubVisionMarkPicker()
        return orig_create_agent(name, blackboard=blackboard)

    DI.agent_factory.create_agent = _create_agent
    try:
        tool_cfg = DI.tool_registry.get_tool("web_page_coords")
        assert tool_cfg and tool_cfg.get("tool_class"), "web_page_coords tool not registered"
        tool = tool_cfg["tool_class"]()

        # Use a question that strongly biases scoring toward the textbox label.
        res = tool.execute(
            ToolMessage(
                tool_name="web_page_coords",
                tool_data={
                    "tool_name": "web_page_coords",
                    "arguments": {
                        "question": "Find the Item Search textbox and return its coordinates",
                        
                        "strict": True,
                    },
                },
            )
        )
        assert res.result_type == "web_page_coords"
        assert isinstance(res.data, dict)
        assert res.data.get("marked") is True
        assert int(res.data.get("marks_count") or 0) >= 1

        # Validate: some mark corresponds to the input by checking elementFromPoint at mark centers.
        js_find = """
async (page) => {
  return await page.evaluate(() => {
    const mm = (window.__emi_marks_map && Array.isArray(window.__emi_marks_map)) ? window.__emi_marks_map : [];
    function norm(s){ return String(s||"").toLowerCase(); }

    for (const m of mm) {
      if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
      const label = norm(m.label || "");
      // Prefer the mark that says Item Search if present.
      if (label.includes("item search") || label.includes("search items")) {
        const el = document.elementFromPoint(m.x, m.y);
        const tag = el ? String(el.tagName || "") : "";
        const id = el ? (el.id || null) : null;
        return { ok: true, tag, id, label: m.label || null, x: m.x, y: m.y };
      }
    }

    // Fallback: find any mark center that hits an input/textarea/contenteditable element.
    for (const m of mm) {
      if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
      const el = document.elementFromPoint(m.x, m.y);
      if (!el) continue;
      const tag = String(el.tagName || "").toLowerCase();
      const isCE = !!el.isContentEditable;
      const role = (el.getAttribute && el.getAttribute("role")) ? String(el.getAttribute("role")).toLowerCase() : "";
      if (tag === "input" || tag === "textarea" || isCE || role === "textbox") {
        return { ok: true, tag: String(el.tagName || ""), id: el.id || null, label: m.label || null, x: m.x, y: m.y };
      }
    }
    return { ok: false, reason: "no textbox hit by any mark center", marks: mm.slice(0, 10) };
  });
}
""".strip()
        resp = mcp_stdio_call_tool(
            server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_find}, timeout_s=20
        )
        text_items = ((resp or {}).get("result") or {}).get("content") or []
        text = ""
        for it in text_items:
            if isinstance(it, dict) and it.get("type") == "text":
                text = it.get("text") or ""
                break
        info = _parse_jsonish(text)
        assert isinstance(info, dict) and info.get("ok") is True, f"Did not find textbox mark. raw={text!r}"
        assert str(info.get("tag") or "").upper() in {"INPUT", "TEXTAREA"} or str(info.get("tag") or "").lower() in {
            "input",
            "textarea",
        }, f"Expected input-like element at mark center. got={info!r}"
    finally:
        DI.agent_factory.create_agent = orig_create_agent


def test_web_page_coords_includes_modal_address_textbox_mark():
    """
    Regression for the reported issue: "addresses input is never highlighted in the modal".

    Ensure a textbox inside the modal itself gets marked and that the mark center hits the modal input.
    """
    _ensure_playwright_mcp_tools()

    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"

    _set_busy_test_page(server_entry)

    # Patch agent factory so this test does not depend on OpenAI.
    orig_create_agent = DI.agent_factory.create_agent

    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_mark_picker":
            return _StubVisionMarkPicker()
        return orig_create_agent(name, blackboard=blackboard)

    DI.agent_factory.create_agent = _create_agent
    try:
        tool_cfg = DI.tool_registry.get_tool("web_page_coords")
        assert tool_cfg and tool_cfg.get("tool_class"), "web_page_coords tool not registered"
        tool = tool_cfg["tool_class"]()

        res = tool.execute(
            ToolMessage(
                tool_name="web_page_coords",
                tool_data={
                    "tool_name": "web_page_coords",
                    "arguments": {
                        "question": "Find the address input textbox in the Addresses modal",
                        
                        "strict": True,
                    },
                },
            )
        )
        assert res.result_type == "web_page_coords"
        assert isinstance(res.data, dict)
        assert res.data.get("marked") is True
        assert int(res.data.get("marks_count") or 0) >= 1

        js_find = """
async (page) => {
  return await page.evaluate(() => {
    const mm = (window.__emi_marks_map && Array.isArray(window.__emi_marks_map)) ? window.__emi_marks_map : [];
    function norm(s){ return String(s||"").toLowerCase(); }

    // Prefer mark labels that look like the modal address input.
    for (const m of mm) {
      if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
      const label = norm(m.label || "");
      if (!(label.includes("address") || label.includes("enter address"))) continue;
      const el = document.elementFromPoint(m.x, m.y);
      if (!el) continue;
      const inp = el.closest ? el.closest("input,textarea,[contenteditable='true'],[role='textbox'],[role='combobox']") : null;
      if (inp) {
        return { ok: true, hit: (inp.tagName || null), id: inp.id || null, label: m.label || null };
      }
    }

    // Fallback: any mark that hits our specific modal input id.
    for (const m of mm) {
      if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
      const el = document.elementFromPoint(m.x, m.y);
      if (!el) continue;
      const inp = el.closest ? el.closest("input#modal-address-search") : null;
      if (inp) {
        return { ok: true, hit: (inp.tagName || null), id: inp.id || null, label: m.label || null };
      }
    }

    return { ok: false, reason: "no modal textbox hit by any mark center", marks: mm.slice(0, 12) };
  });
}
""".strip()
        resp = mcp_stdio_call_tool(
            server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_find}, timeout_s=20
        )
        text_items = ((resp or {}).get("result") or {}).get("content") or []
        text = ""
        for it in text_items:
            if isinstance(it, dict) and it.get("type") == "text":
                text = it.get("text") or ""
                break
        info = _parse_jsonish(text)
        assert isinstance(info, dict) and info.get("ok") is True, f"Did not find modal address textbox mark. raw={text!r}"
        assert str(info.get("id") or "") == "modal-address-search"
    finally:
        DI.agent_factory.create_agent = orig_create_agent


def test_web_fill_xy_atomic_click_type_enter_sets_input_value():
    """
    Basic integration test for `web_fill_xy`:
    - click an input by coords
    - clear + type
    - press Enter
    - verify DOM sees the value and an Enter handler fired
    """
    _ensure_playwright_mcp_tools()

    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"

    html = r"""
<!doctype html>
<html>
<head><meta charset="utf-8"/><title>Fill XY Test</title></head>
<body style="font-family: Arial; padding: 24px;">
  <label for="q" style="display:block; font-weight:700; margin-bottom:6px;">Search</label>
  <input id="q" aria-label="Search" placeholder="Type here..." style="width: 420px; padding: 10px 12px; border-radius: 12px; border: 1px solid #cbd5e1;" />
  <div id="status" style="margin-top: 12px; color: #0f172a;"></div>
  <script>
    const q = document.getElementById('q');
    q.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        document.getElementById('status').textContent = 'ENTER:' + (q.value || '');
      }
    });
  </script>
</body>
</html>
"""
    js_set = f"""
async (page) => {{
  await page.setViewportSize({{ width: {int(VIEWPORT_WIDTH)}, height: {int(VIEWPORT_HEIGHT)} }});
  await page.setContent({json.dumps(html)}, {{ waitUntil: 'domcontentloaded' }});
  return {{ ok: true }};
}}
""".strip()
    _ = mcp_stdio_call_tool(server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_set}, timeout_s=30)

    # Find the input center coords.
    js_rect = """
async (page) => {
  return await page.evaluate(() => {
    const el = document.getElementById('q');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width/2, y: r.top + r.height/2 };
  });
}
""".strip()
    resp = mcp_stdio_call_tool(server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_rect}, timeout_s=20)
    text_items = ((resp or {}).get("result") or {}).get("content") or []
    text = ""
    for it in text_items:
        if isinstance(it, dict) and it.get("type") == "text":
            text = it.get("text") or ""
            break
    pt = _parse_jsonish(text)
    assert isinstance(pt, dict) and isinstance(pt.get("x"), (int, float)) and isinstance(pt.get("y"), (int, float))

    tool_cfg = DI.tool_registry.get_tool("web_fill_xy")
    assert tool_cfg and tool_cfg.get("tool_class"), "web_fill_xy tool not registered"
    tool = tool_cfg["tool_class"]()

    res = tool.execute(
        ToolMessage(
            tool_name="web_fill_xy",
            tool_data={
                "tool_name": "web_fill_xy",
                "arguments": {"x": float(pt["x"]), "y": float(pt["y"]), "text": "cheeseburger", "submit": True, "clear_first": True},
            },
        )
    )
    assert res.result_type == "web_fill_xy", f"unexpected result: {res}"

    # Verify Enter handler fired.
    js_check = """
async (page) => {
  return await page.evaluate(() => {
    return {
      value: (document.getElementById('q') && document.getElementById('q').value) || null,
      status: (document.getElementById('status') && document.getElementById('status').textContent) || null,
    };
  });
}
""".strip()
    resp2 = mcp_stdio_call_tool(server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js_check}, timeout_s=20)
    text_items2 = ((resp2 or {}).get("result") or {}).get("content") or []
    text2 = ""
    for it in text_items2:
        if isinstance(it, dict) and it.get("type") == "text":
            text2 = it.get("text") or ""
            break
    got = _parse_jsonish(text2)
    assert isinstance(got, dict)
    assert got.get("value") == "cheeseburger"
    assert str(got.get("status") or "").startswith("ENTER:cheeseburger")


def main() -> int:
    """
    Allow running this test directly without pytest:

      python app/assistant/test/test_web_page_coords_marks.py
    """
    test_web_page_coords_marks_returns_clickable_coords_for_modal_close()
    print("✅ test_web_page_coords_marks_returns_clickable_coords_for_modal_close passed")
    test_web_page_coords_hover_probe_prefers_large_hover_revealed_tile_over_heart()
    print("✅ test_web_page_coords_hover_probe_prefers_large_hover_revealed_tile_over_heart passed")
    test_web_page_coords_marks_map_contains_rects_centered_within_10px()
    print("✅ test_web_page_coords_marks_map_contains_rects_centered_within_10px passed")
    test_web_page_coords_includes_textbox_mark_and_center_hits_input()
    print("✅ test_web_page_coords_includes_textbox_mark_and_center_hits_input passed")
    test_web_page_coords_includes_modal_address_textbox_mark()
    print("✅ test_web_page_coords_includes_modal_address_textbox_mark passed")
    test_web_fill_xy_atomic_click_type_enter_sets_input_value()
    print("✅ test_web_fill_xy_atomic_click_type_enter_sets_input_value passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

