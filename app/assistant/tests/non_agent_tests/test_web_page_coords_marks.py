"""Live tests: web_page_coords marks pipeline and web_fill_xy against a real
(isolated, headless) browser. The vision mark picker is stubbed.

web_page_coords clears window.__emi_marks_map when it removes its overlay before
returning, so tests that inspect the marks capture them from inside the stubbed
picker, which runs while the marks are live.
"""
import json

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.tests.non_agent_tests.playwright_live import (
    click_xy,
    element_at,
    evaluate,
    live_marks_map,
    live_server_entry,  # noqa: F401 (pytest fixture)
    load_page,
    requires_live_playwright,
)
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

pytestmark = requires_live_playwright


VIEWPORT_WIDTH = 2048
VIEWPORT_HEIGHT = 1400


_BUSY_PAGE_HTML = r"""
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


def _load_busy_page(server_entry: dict) -> None:
    load_page(server_entry, _BUSY_PAGE_HTML, VIEWPORT_WIDTH, VIEWPORT_HEIGHT)


class _CapturingMarkPicker:
    """Deterministic vision stub: always picks mark 1, and records the live marks map.

    The busy test page is designed so the modal close button is highest-scored -> mark id 1.
    """

    def __init__(self, server_entry: dict):
        self._server_entry = server_entry
        self.marks: list[dict] = []

    def action_handler(self, _msg):
        self.marks = live_marks_map(self._server_entry)
        return ToolResult(
            result_type="llm_result",
            content="stub mark picker",
            data={"action": "done", "mark_ids": [1], "confidence": 1.0, "rationale": "always pick mark 1"},
        )


def _run_web_page_coords(server_entry: dict, question: str) -> tuple[ToolResult, _CapturingMarkPicker]:
    """Run the real web_page_coords tool with the vision picker stubbed (no LLM)."""
    picker = _CapturingMarkPicker(server_entry)
    orig_create_agent = DI.agent_factory.create_agent

    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_mark_picker":
            return picker
        return orig_create_agent(name, blackboard=blackboard)

    DI.agent_factory.create_agent = _create_agent
    try:
        tool_cfg = DI.tool_registry.get_tool("web_page_coords")
        assert tool_cfg and tool_cfg.get("tool_class"), "web_page_coords tool not registered"
        res = tool_cfg["tool_class"]().execute(
            ToolMessage(
                tool_name="web_page_coords",
                tool_data={"tool_name": "web_page_coords", "arguments": {"question": question, "strict": True}},
            )
        )
    finally:
        DI.agent_factory.create_agent = orig_create_agent
    assert res.result_type == "web_page_coords", f"Unexpected result: {res.content!r}"
    assert isinstance(res.data, dict)
    return res, picker


def test_web_page_coords_marks_returns_clickable_coords_for_modal_close(live_server_entry):  # noqa: F811
    _load_busy_page(live_server_entry)

    res, _picker = _run_web_page_coords(
        live_server_entry, "Find the close (X) button on the open Addresses modal and return its coordinates"
    )
    assert res.data.get("marked") is True
    targets = res.data.get("targets")
    assert isinstance(targets, list) and targets, "Expected at least 1 target"
    x = float(targets[0]["x"])
    y = float(targets[0]["y"])

    # The agent ONLY ever sees `content` (the recent-history renderer renders
    # `content` and drops `data`), so the resolved coords MUST appear in
    # `content` — otherwise the planner is blind and falls back to guessing
    # pixel positions (the 2026-06-14 bug that drove a 240-cycle click loop).
    assert f"x={int(round(x))}" in res.content and f"y={int(round(y))}" in res.content, (
        f"web_page_coords.content must carry the target coords for the agent to "
        f"click them; got: {res.content!r}"
    )

    # The returned coords point at the actual close button.
    info = element_at(live_server_entry, x, y)
    assert info.get("id") == "modal-close" or "close" in str(info.get("aria") or "").lower(), info

    # Click and verify the modal disappears.
    click_xy(live_server_entry, x, y)
    present = evaluate(live_server_entry, "() => ({ present: Boolean(document.getElementById('addresses-modal')) })")
    assert isinstance(present, dict) and present.get("present") is False


def test_web_page_coords_marks_map_contains_rects_centered_within_10px(live_server_entry):  # noqa: F811
    """The marks map carries `rect` and each mark's (x,y) is near the rect center (±10px)."""
    _load_busy_page(live_server_entry)

    res, picker = _run_web_page_coords(
        live_server_entry, "Find the close (X) button on the open Addresses modal and return its coordinates"
    )
    assert res.data.get("marked") is True
    assert int(res.data.get("marks_count") or 0) >= 1

    mm = picker.marks[:8]
    assert mm, "Expected a non-empty __emi_marks_map while the tool was running"
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


def test_web_page_coords_includes_textbox_mark_and_center_hits_input(live_server_entry):  # noqa: F811
    """Textbox candidates are marked, and a mark center lands on the Item Search <input>."""
    _load_busy_page(live_server_entry)
    # Remove the modal overlay so the textbox is actually hittable by elementFromPoint.
    evaluate(
        live_server_entry,
        "() => { const m = document.getElementById('addresses-modal'); if (m) m.remove(); return { ok: true }; }",
    )

    res, picker = _run_web_page_coords(live_server_entry, "Find the Item Search textbox and return its coordinates")
    assert res.data.get("marked") is True
    assert int(res.data.get("marks_count") or 0) >= 1

    js_find = """
() => {
  const mm = %s;
  function norm(s){ return String(s||"").toLowerCase(); }

  for (const m of mm) {
    if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
    const label = norm(m.label || "");
    // Prefer the mark that says Item Search if present.
    if (label.includes("item search") || label.includes("search items")) {
      const el = document.elementFromPoint(m.x, m.y);
      return { ok: true, tag: el ? String(el.tagName || "") : "", id: el ? (el.id || null) : null, label: m.label || null };
    }
  }

  // Otherwise: any mark center that hits an input/textarea/contenteditable element.
  for (const m of mm) {
    if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
    const el = document.elementFromPoint(m.x, m.y);
    if (!el) continue;
    const tag = String(el.tagName || "").toLowerCase();
    const role = (el.getAttribute && el.getAttribute("role")) ? String(el.getAttribute("role")).toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || el.isContentEditable || role === "textbox") {
      return { ok: true, tag: String(el.tagName || ""), id: el.id || null, label: m.label || null };
    }
  }
  return { ok: false, reason: "no textbox hit by any mark center", marks: mm.slice(0, 10) };
}
""" % json.dumps(picker.marks)
    info = evaluate(live_server_entry, js_find)
    assert isinstance(info, dict) and info.get("ok") is True, f"Did not find textbox mark: {info!r}"
    assert str(info.get("tag") or "").lower() in {"input", "textarea"}, f"Expected input-like element at mark center. got={info!r}"


def test_web_page_coords_includes_modal_address_textbox_mark(live_server_entry):  # noqa: F811
    """Regression: "addresses input is never highlighted in the modal".

    A textbox inside the modal itself gets marked and its mark center hits the modal input.
    """
    _load_busy_page(live_server_entry)

    res, picker = _run_web_page_coords(live_server_entry, "Find the address input textbox in the Addresses modal")
    assert res.data.get("marked") is True
    assert int(res.data.get("marks_count") or 0) >= 1

    js_find = """
() => {
  const mm = %s;
  function norm(s){ return String(s||"").toLowerCase(); }

  // Prefer mark labels that look like the modal address input.
  for (const m of mm) {
    if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
    const label = norm(m.label || "");
    if (!(label.includes("address") || label.includes("enter address"))) continue;
    const el = document.elementFromPoint(m.x, m.y);
    if (!el) continue;
    const inp = el.closest ? el.closest("input,textarea,[contenteditable='true'],[role='textbox'],[role='combobox']") : null;
    if (inp) return { ok: true, hit: (inp.tagName || null), id: inp.id || null, label: m.label || null };
  }

  // Otherwise: any mark that hits our specific modal input id.
  for (const m of mm) {
    if (!m || typeof m.x !== "number" || typeof m.y !== "number") continue;
    const el = document.elementFromPoint(m.x, m.y);
    if (!el) continue;
    const inp = el.closest ? el.closest("input#modal-address-search") : null;
    if (inp) return { ok: true, hit: (inp.tagName || null), id: inp.id || null, label: m.label || null };
  }

  return { ok: false, reason: "no modal textbox hit by any mark center", marks: mm.slice(0, 12) };
}
""" % json.dumps(picker.marks)
    info = evaluate(live_server_entry, js_find)
    assert isinstance(info, dict) and info.get("ok") is True, f"Did not find modal address textbox mark: {info!r}"
    assert str(info.get("id") or "") == "modal-address-search"


_FILL_PAGE_HTML = r"""
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


def test_web_fill_xy_atomic_click_type_enter_sets_input_value(live_server_entry):  # noqa: F811
    """web_fill_xy: click an input by coords, clear + type, press Enter; the DOM sees the value and the Enter handler fired."""
    load_page(live_server_entry, _FILL_PAGE_HTML, VIEWPORT_WIDTH, VIEWPORT_HEIGHT)

    pt = evaluate(
        live_server_entry,
        "() => { const r = document.getElementById('q').getBoundingClientRect(); return { x: r.left + r.width/2, y: r.top + r.height/2 }; }",
    )
    assert isinstance(pt, dict) and isinstance(pt.get("x"), (int, float)) and isinstance(pt.get("y"), (int, float))

    tool_cfg = DI.tool_registry.get_tool("web_fill_xy")
    assert tool_cfg and tool_cfg.get("tool_class"), "web_fill_xy tool not registered"
    res = tool_cfg["tool_class"]().execute(
        ToolMessage(
            tool_name="web_fill_xy",
            tool_data={
                "tool_name": "web_fill_xy",
                "arguments": {"x": float(pt["x"]), "y": float(pt["y"]), "text": "cheeseburger", "submit": True, "clear_first": True},
            },
        )
    )
    assert res.result_type == "web_fill_xy", f"unexpected result: {res}"

    got = evaluate(
        live_server_entry,
        "() => ({ value: document.getElementById('q').value || null, status: document.getElementById('status').textContent || null })",
    )
    assert isinstance(got, dict)
    assert got.get("value") == "cheeseburger"
    assert str(got.get("status") or "").startswith("ENTER:cheeseburger")
