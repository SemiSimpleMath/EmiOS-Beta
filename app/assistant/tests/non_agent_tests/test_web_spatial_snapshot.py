"""Live test: web_spatial_snapshot against a real (isolated, headless) browser."""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.tests.non_agent_tests.playwright_live import (
    live_server_entry,  # noqa: F401 (pytest fixture)
    load_page,
    requires_live_playwright,
)
from app.assistant.utils.pydantic_classes import ToolMessage

pytestmark = requires_live_playwright


_BUSY_PAGE_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Spatial Snapshot Test Page</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; }
    header { background: #111827; color: #fff; padding: 14px 18px; }
    .grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; padding: 16px; }
    .btn { padding: 10px 8px; border-radius: 10px; border: 1px solid #cbd5e1; background: #f8fafc; cursor: pointer; }
    #addresses-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: flex; align-items: center; justify-content: center; }
    #modal-card { width: 520px; background: white; border-radius: 14px; box-shadow: 0 10px 35px rgba(0,0,0,0.35); padding: 18px 18px 14px; position: relative; }
    #modal-title { font-size: 18px; font-weight: 700; margin: 0 0 8px; }
    #modal-close { position: absolute; top: 12px; right: 12px; width: 34px; height: 34px; border-radius: 10px; border: 1px solid #e2e8f0; background: #fff; cursor: pointer; }
  </style>
</head>
<body>
  <header>Header</header>
  <main class="grid" id="main-grid"></main>

  <div id="addresses-modal" role="dialog" aria-label="Addresses modal">
    <div id="modal-card">
      <button id="modal-close" aria-label="Close Addresses modal">×</button>
      <div id="modal-title">Addresses</div>
      <div>Pick an address to continue</div>
      <button class="btn" aria-label="Confirm address">Confirm</button>
    </div>
  </div>

  <script>
    const grid = document.getElementById('main-grid');
    for (let i = 1; i <= 24; i++) {
      const b = document.createElement('button');
      b.className = 'btn';
      b.textContent = 'Button ' + i;
      b.setAttribute('aria-label', 'Background Button ' + i);
      grid.appendChild(b);
    }
  </script>
</body>
</html>
"""


def test_web_spatial_snapshot_returns_anchors_and_nearby_text(live_server_entry):  # noqa: F811
    load_page(live_server_entry, _BUSY_PAGE_HTML, 900, 650)

    tool_cfg = DI.tool_registry.get_tool("web_spatial_snapshot")
    assert tool_cfg and tool_cfg.get("tool_class"), "web_spatial_snapshot tool not registered"
    tool = tool_cfg["tool_class"]()

    res = tool.execute(
        ToolMessage(
            tool_name="web_spatial_snapshot",
            tool_data={
                "tool_name": "web_spatial_snapshot",
                "arguments": {"question": "close addresses modal", "radius_px": 200, "max_anchors": 80, "per_anchor_nearby": 5},
            },
        )
    )
    assert res.result_type == "web_spatial_snapshot", f"Unexpected result: {res.content!r}"
    assert isinstance(res.data, dict)
    anchors = res.data.get("anchors")
    assert isinstance(anchors, list) and anchors, "Expected anchors list"

    # Expect to find our close button via aria-label, with nearby text context.
    close = next(
        (a for a in anchors if isinstance(a, dict) and "close addresses" in str(a.get("label") or "").lower()),
        None,
    )
    assert close is not None, "Did not find Close Addresses modal anchor"
    assert isinstance(close.get("nearby_text"), list)
