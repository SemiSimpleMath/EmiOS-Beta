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
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


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


def _set_simple_page(server_entry: dict) -> None:
    html = r"""
<!doctype html>
<html>
<head><meta charset="utf-8" /><title>Scout Test</title></head>
<body>
  <h1>Restaurants</h1>
  <div style="display:flex; gap:16px;">
    <div style="width:260px; height:160px; border:1px solid #ccc; border-radius:12px; background:#f8fafc;">
      <img alt="Burger photo" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/%3E" />
      <button aria-label="Add this store to your saved list">♥</button>
      <div>The Cut Handcrafted Burgers</div>
    </div>
  </div>
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


class _StubVisionProseScout:
    def action_handler(self, _msg):
        return ToolResult(
            result_type="llm_result",
            content="stub prose scout",
            data={
                "page_overview": "Page shows a Restaurants header and a restaurant card with a burger photo and a heart/favorite button.",
                "blockers": [],
                "suggested_next_steps": ["Click the restaurant card to open menu", "Avoid clicking the heart favorite button"],
                "things_to_look_for_in_snapshot": ["Open Menu", "Add to cart"],
            },
        )


def test_web_visual_scout_uses_screenshot_and_returns_prose():
    _ensure_playwright_mcp_tools()
    server_entry = DI.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
    assert isinstance(server_entry, dict), "Missing MCP server entry npm/playwright-mcp"
    _set_simple_page(server_entry)

    # Patch agent factory so this test does not depend on OpenAI.
    orig_create_agent = DI.agent_factory.create_agent

    def _create_agent(name, blackboard=None):  # noqa: ARG001
        if name == "shared::vision_prose_scout":
            return _StubVisionProseScout()
        return orig_create_agent(name, blackboard=blackboard)

    DI.agent_factory.create_agent = _create_agent
    try:
        tool_cfg = DI.tool_registry.get_tool("web_visual_scout")
        assert tool_cfg and tool_cfg.get("tool_class"), "web_visual_scout tool not registered"
        tool = tool_cfg["tool_class"]()
        res = tool.execute(
            ToolMessage(
                tool_name="web_visual_scout",
                tool_data={"tool_name": "web_visual_scout", "arguments": {"question": "Describe the page UI", "full_page": False}},
            )
        )
        assert res.result_type == "web_visual_scout", f"Unexpected result_type={res.result_type!r} content={res.content!r}"
        assert isinstance(res.data, dict)
        assert isinstance(res.data.get("image_path"), str) and res.data["image_path"].lower().endswith(".png")
        scout = res.data.get("scout")
        assert isinstance(scout, dict)
        assert "Restaurants" in (scout.get("page_overview") or "") or "restaurant" in (scout.get("page_overview") or "").lower()
    finally:
        DI.agent_factory.create_agent = orig_create_agent


def main() -> int:
    test_web_visual_scout_uses_screenshot_and_returns_prose()
    print("✅ test_web_visual_scout_uses_screenshot_and_returns_prose passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

