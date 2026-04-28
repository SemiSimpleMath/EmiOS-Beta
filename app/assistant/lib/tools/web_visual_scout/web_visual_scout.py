from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)


def _pick_first_image_path(attachments: list[dict[str, Any]] | None) -> Optional[str]:
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "image":
            continue
        p = a.get("path")
        if isinstance(p, str) and p.strip():
            try:
                return str(Path(p).resolve())
            except Exception:
                return p.strip()
    return None


class WebVisualScout(BaseTool):
    """
    Prose-only visual scout for Playwright MCP browsing.

    - Takes a screenshot of the current page (viewport by default)
    - Runs `shared::vision_prose_scout` to generate a detailed prose description
      of layout, blockers, carousels/slide windows, image tiles, etc.

    IMPORTANT:
    - This tool does NOT return coordinates.
    - Use this when the planner is stuck and needs a "human-like" description of the UI.
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_SCREENSHOT = "browser_take_screenshot"
    SCOUT_AGENT = "shared::vision_prose_scout"

    def __init__(self):
        super().__init__("web_visual_scout")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        question = (args.get("question") or "").strip()
        full_page = bool(args.get("full_page", False))

        # Resolve server entry via shared ToolRegistry instance
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None

        if not isinstance(server_entry, dict):
            return ToolResult(
                result_type="error",
                content=f"web_visual_scout error: MCP server entry missing for {self.SERVER_ID}",
                data={"server_id": self.SERVER_ID},
            )

        # 1) Screenshot
        attachments: list[dict[str, Any]] = []
        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                call_resp = mcp_stdio_call_tool(
                    server_entry=server_entry,
                    tool_name=self.MCP_SCREENSHOT,
                    arguments={"type": "png", "fullPage": bool(full_page)},
                    timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
                )
                _text, is_error, attachments = format_mcp_tool_result_content(call_resp)
                if is_error:
                    raise RuntimeError(_text or "MCP screenshot returned isError")
                last_err = None
                break
            except Exception as e:
                last_err = e
                # Small backoff helps when Playwright MCP is still spawning.
                try:
                    time.sleep(0.6)
                except Exception:
                    pass

        if last_err is not None:
            return ToolResult(
                result_type="error",
                content=f"web_visual_scout error: screenshot failed (attempts=2): {last_err}",
                data={"tool": self.MCP_SCREENSHOT, "full_page": bool(full_page)},
            )

        image_path = _pick_first_image_path(attachments)
        if not image_path:
            return ToolResult(
                result_type="error",
                content="web_visual_scout error: screenshot produced no persisted image attachment",
                data={"attachments": attachments},
            )

        # 2) Vision prose scout
        scout = DI.agent_factory.create_agent(self.SCOUT_AGENT)
        if scout is None:
            return ToolResult(
                result_type="error",
                content=f"web_visual_scout error: could not create agent {self.SCOUT_AGENT}",
                data={"agent": self.SCOUT_AGENT},
            )

        task = question or "Describe this page in detail (layout, blockers, carousels, image tiles, what seems clickable)."
        msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "task": task,
                "image": image_path,
            }
        )
        res = scout.action_handler(msg)

        payload: dict[str, Any] = {}
        if hasattr(res, "data") and isinstance(res.data, dict):
            payload = res.data
        elif isinstance(res, dict):
            payload = res
        else:
            payload = {"raw": str(res)}

        overview = payload.get("page_overview") if isinstance(payload.get("page_overview"), str) else ""
        blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []

        content_lines = ["web_visual_scout:"]
        content_lines.append(f"- image_path: {image_path}")
        if overview:
            content_lines.append("- page_overview:")
            content_lines.append(overview.strip())
        if blockers:
            content_lines.append(f"- blockers: {blockers}")

        return ToolResult(
            result_type="web_visual_scout",
            content="\n".join(content_lines).strip(),
            data={
                "question": question or None,
                "full_page": bool(full_page),
                "image_path": image_path,
                "attachments": attachments,
                "scout": payload,
            },
        )


def get_tool_class():
    return WebVisualScout

