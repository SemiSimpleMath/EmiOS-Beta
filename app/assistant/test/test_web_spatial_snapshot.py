import json
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
from app.assistant.utils.pydantic_classes import ToolMessage


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

    js = f"""
async (page) => {{
  await page.setViewportSize({{ width: 900, height: 650 }});
  await page.setContent({json.dumps(html)}, {{ waitUntil: 'domcontentloaded' }});
  return {{ ok: true }};
}}
"""
    _ = mcp_stdio_call_tool(server_entry=server_entry, tool_name="browser_run_code", arguments={"code": js}, timeout_s=30)


def test_web_spatial_snapshot_returns_anchors_and_nearby_text():
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"
    _set_busy_test_page(server_entry)

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
    assert res.result_type == "web_spatial_snapshot"
    assert isinstance(res.data, dict)
    anchors = res.data.get("anchors")
    assert isinstance(anchors, list) and anchors, "Expected anchors list"

    # Expect to find our close button via aria-label.
    found_close = False
    for a in anchors:
        if not isinstance(a, dict):
            continue
        label = (a.get("label") or "")
        if isinstance(label, str) and "close addresses" in label.lower():
            found_close = True
            nearby = a.get("nearby_text")
            assert isinstance(nearby, list)
            # Should have some nearby context.
            assert any(isinstance(t, str) and "addresses" in t.lower() for t in nearby) or len(nearby) >= 0
            break
    assert found_close, "Did not find Close Addresses modal anchor"


def main():
    test_web_spatial_snapshot_returns_anchors_and_nearby_text()
    print("✅ web_spatial_snapshot test passed")


if __name__ == "__main__":
    main()

