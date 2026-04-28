from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.utils.pipeline_state import (
    get_flag,
    get_last_tool_result_meta,
    set_flag,
    set_pending_tool,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PlaywrightPageOverviewNode(ControlNode):
    """
    Deterministic Playwright page overview:
    - Resize viewport once per session (slightly taller).
    - Run playwright_page_overview (top + one-viewport-down prose).

    RESIZE TRADEOFF — read before changing TARGET_WIDTH/TARGET_HEIGHT:
    ------------------------------------------------------------------
    browser_resize enlarges the CSS viewport so more content is visible
    in a single web_page_coords call (e.g. more DoorDash restaurant tiles
    fit without scrolling). That is the ONLY benefit.

    The cost: browser_take_screenshot captures at the PHYSICAL display's
    native pixel resolution (capped by Windows DPI scaling), not at the
    CSS viewport size. This means the screenshot PNG is smaller than the
    CSS viewport, which shrinks the numbered badge overlays in the image:

        badge_px_in_png = 14 * (png_width / css_viewport_width)

    Measured on a 1920×1080 / 150% DPI display (png cap ≈ 1464px wide):
        1280 CSS → 1280 PNG → badge = 14.0px  ✅ clear
        1600 CSS → 1389 PNG → badge = 12.2px  ✅ clear
        1920 CSS → 1464 PNG → badge = 10.7px  ✅ clear
        2048 CSS → 1434 PNG → badge ≈  9.8px  ⚠ marginal (current target)
        2560 CSS → 1464 PNG → badge =  8.0px  ⚠ marginal (soft threshold)
        3840 CSS → 1464 PNG → badge =  5.3px  ❌ unreadable

    VERDICT: 2048×1400 is the practical safe maximum on a 1080p display.
    Do NOT raise TARGET_WIDTH above 2560 — badges become unreadable and
    the vision model will hallucinate wrong badge numbers, causing
    OUT-OF-RANGE MARK ID errors in web_page_coords.

    Click accuracy (DOM CSS coords) is NOT affected by the resize — the
    stored (x, y) values are always element centers in CSS pixels and
    remain correct regardless of PNG scaling.
    ------------------------------------------------------------------
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_RESIZE = "browser_resize"
    RESIZE_FLAG = "playwright_viewport_resized"

    TARGET_WIDTH = 2048
    TARGET_HEIGHT = 1400

    def action_handler(self, message):  # noqa: ARG002
        try:
            self.blackboard.update_state_value("next_agent", None)
        except Exception as e:
            logger.debug("[%s] Failed to clear next_agent at node start: %s", self.name, e, exc_info=True)

        if not bool(get_flag(self.blackboard, self.RESIZE_FLAG, False)):
            server_entry = None
            try:
                server_entry = self.tool_registry.get_mcp_server_entry(self.SERVER_ID)
            except Exception as e:
                logger.debug("[%s] Failed to read MCP server entry: %s", self.name, e, exc_info=True)
                server_entry = None

            if not isinstance(server_entry, dict):
                logger.warning("[%s] Missing MCP server entry for %s", self.name, self.SERVER_ID)
            else:
                try:
                    call_resp = mcp_stdio_call_tool(
                        server_entry=server_entry,
                        tool_name=self.MCP_RESIZE,
                        arguments={
                            "width": int(self.TARGET_WIDTH),
                            "height": int(self.TARGET_HEIGHT),
                        },
                        timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
                    )
                    _text, is_error, _attachments = format_mcp_tool_result_content(call_resp)
                    if is_error:
                        logger.warning("[%s] browser_resize returned error: %s", self.name, _text or "unknown error")
                    else:
                        set_flag(self.blackboard, self.RESIZE_FLAG, True)

                    # Reset browser zoom to 100%.
                    # User zoom (Ctrl+scroll) shrinks window.innerWidth/innerHeight in CSS pixels,
                    # causing web_page_coords to filter out elements near the viewport edges.
                    # document.body.style.zoom is not standard across all browsers, but Playwright
                    # Chromium supports it. Best-effort: log a warning on failure, never crash.
                    try:
                        zoom_js = (
                            "async (page) => {\n"
                            "  return await page.evaluate(() => {\n"
                            "    try {\n"
                            "      document.body.style.zoom = '100%';\n"
                            "      return { ok: true, zoom: document.body.style.zoom };\n"
                            "    } catch (e) {\n"
                            "      return { ok: false, error: String(e) };\n"
                            "    }\n"
                            "  });\n"
                            "}"
                        )
                        zoom_resp = mcp_stdio_call_tool(
                            server_entry=server_entry,
                            tool_name="browser_run_code",
                            arguments={"code": zoom_js},
                            timeout_s=10,
                        )
                        _zt, _ze, _ = format_mcp_tool_result_content(zoom_resp)
                        if _ze:
                            logger.warning("[%s] zoom reset returned error: %s", self.name, _zt or "unknown")
                        else:
                            logger.debug("[%s] zoom reset: %s", self.name, (_zt or "")[:80])
                    except Exception as e:
                        logger.warning("[%s] zoom reset failed (non-fatal): %s", self.name, e)
                except Exception as e:
                    logger.warning("[%s] browser_resize failed: %s", self.name, e)
                    logger.debug("[%s] browser_resize exception details", self.name, exc_info=True)

        try:
            last_meta = get_last_tool_result_meta(self.blackboard) or {}
            calling_agent = last_meta.get("calling_agent")
            if not isinstance(calling_agent, str) or not calling_agent.strip():
                raise ValueError(
                    f"[{self.name}] Missing last_tool_result_meta.calling_agent; "
                    "cannot schedule playwright_page_overview without a resume target."
                )
            set_pending_tool(
                self.blackboard,
                name="playwright_page_overview",
                calling_agent=calling_agent.strip(),
                action_input=None,
                arguments={
                    "wait_seconds": 5,
                    "wait_for_dom_ready": True,
                    "scroll_pause_seconds": 1.2,
                    "resize_viewport": False,
                },
                kind="tool",
            )
            self.blackboard.update_state_value("next_agent", "tool_caller")
            self.blackboard.update_state_value("last_agent", self.name)
        except Exception as e:
            logger.warning("[%s] Failed to schedule playwright_page_overview: %s", self.name, e)
            logger.debug("[%s] page overview scheduling exception details", self.name, exc_info=True)
            self.blackboard.update_state_value("error", True)
            self.blackboard.update_state_value("error_message", str(e))