"""Live test: web_visual_scout screenshots a real (isolated, headless) browser.

The vision agent is stubbed so the test does not depend on an LLM.
"""
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.tests.non_agent_tests.playwright_live import (
    live_server_entry,  # noqa: F401 (pytest fixture)
    load_page,
    requires_live_playwright,
)
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

pytestmark = requires_live_playwright


_SIMPLE_PAGE_HTML = r"""
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


def test_web_visual_scout_uses_screenshot_and_returns_prose(live_server_entry):  # noqa: F811
    load_page(live_server_entry, _SIMPLE_PAGE_HTML, 900, 650)

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
        assert "restaurant" in (scout.get("page_overview") or "").lower()
    finally:
        DI.agent_factory.create_agent = orig_create_agent
