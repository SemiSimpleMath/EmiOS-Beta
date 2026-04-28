from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _pick_first_image_path(attachments: list[dict[str, Any]] | None) -> Optional[str]:
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "image":
            continue
        p = a.get("path")
        if isinstance(p, str) and p.strip():
            return p.strip()
    return None


class CriticCaptureNode(ControlNode):
    """
    Deterministically capture a screenshot for the Playwright critic.

    - Calls `browser_take_screenshot` via the Playwright MCP server
    - Stores the persisted image path on the blackboard under `playwright_critic_image`
    - Routes control to `playwright::critic`
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_TOOL = "browser_take_screenshot"

    CRITIC_AGENT = "playwright::critic"

    def action_handler(self, message):
        # Always clear next_agent at start of control nodes
        try:
            self.blackboard.update_state_value("next_agent", None)
        except Exception as e:
            logger.debug("[%s] Failed to clear next_agent in blackboard: %s", self.name, e, exc_info=True)

        # Resolve server entry
        server_entry = None
        try:
            server_entry = self.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None

        if not isinstance(server_entry, dict):
            msg = f"[{self.name}] Missing MCP server entry for {self.SERVER_ID}. Did you load MCP servers/tool cache?"
            logger.error(msg)
            try:
                self.blackboard.update_state_value("playwright_critic_image", None)
                self.blackboard.update_state_value("critic_capture_error", msg)
                self.blackboard.update_state_value("last_agent", self.name)
                self.blackboard.update_state_value("error", True)
            except Exception as e:
                logger.debug("[%s] Failed to write error state to blackboard: %s", self.name, e, exc_info=True)
            return

        # Call MCP screenshot tool
        attachments: list[dict[str, Any]] = []
        try:
            # Prefer viewport screenshot (fast). Full-page is expensive and not needed for modal handling.
            call_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_TOOL,
                arguments={"type": "png", "fullPage": False},
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
            _text, is_error, attachments = format_mcp_tool_result_content(call_resp)
            if is_error:
                raise RuntimeError(_text or "MCP screenshot returned isError")
        except Exception as e:
            msg = f"[{self.name}] Screenshot capture failed: {e}"
            logger.error(msg)
            try:
                self.blackboard.update_state_value("playwright_critic_image", None)
                self.blackboard.update_state_value("critic_capture_error", msg)
                self.blackboard.update_state_value("last_agent", self.name)
                # Do not hard-error the whole manager; let planner attempt recovery.
                self.blackboard.update_state_value("next_agent", self.CRITIC_AGENT)
            except Exception as e:
                logger.debug("[%s] Failed to write screenshot-error state to blackboard: %s", self.name, e, exc_info=True)
            return

        img_path = _pick_first_image_path(attachments)
        if img_path:
            # Normalize to an absolute path if needed.
            try:
                img_path = str(Path(img_path).resolve())
            except Exception as e:
                logger.debug("[%s] Failed to resolve img_path %s: %s", self.name, img_path, e, exc_info=True)

        try:
            self.blackboard.update_state_value("playwright_critic_image", img_path)
        except Exception as e:
            logger.debug("[%s] Failed to write playwright_critic_image to blackboard: %s", self.name, e, exc_info=True)

        # Route to critic next
        try:
            self.blackboard.update_state_value("next_agent", self.CRITIC_AGENT)
            self.blackboard.update_state_value("last_agent", self.name)
        except Exception as e:
            logger.debug("[%s] Failed to write routing state to blackboard: %s", self.name, e, exc_info=True)

