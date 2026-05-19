from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Any, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.time_utils import get_local_time_str
from app.assistant.lib.tools.web_visual_scout.web_visual_scout import WebVisualScout

logger = get_logger(__name__)


class PlaywrightPageOverview(BaseTool):
    """
    Playwright-specific page overview:
    - Wait for page load settle
    - Describe top of page (vision prose scout)
    - Scroll to bottom, describe bottom of page
    - Scroll back to top
    """

    SERVER_ID = "npm/playwright-mcp"
    # playwright-mcp 2026 upgrade: browser_run_code → browser_evaluate.
    MCP_EVALUATE = "browser_evaluate"

    def __init__(self):
        super().__init__("playwright_page_overview")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        wait_seconds = float(args.get("wait_seconds", 5.0) or 5.0)
        wait_for_dom_ready = bool(args.get("wait_for_dom_ready", True))
        scroll_pause_seconds = float(args.get("scroll_pause_seconds", 0.8) or 0.8)
        resize_viewport = bool(args.get("resize_viewport", False))
        top_question = (args.get("top_question") or "").strip()
        bottom_question = (args.get("bottom_question") or "").strip()

        if not top_question:
            top_question = (
                "Describe the top of the page in detail. Focus on layout, headers, "
                "navigation, hero content, and obvious primary actions."
            )
        if not bottom_question:
            bottom_question = (
                "Describe the bottom of the page in detail. Focus on footer content, "
                "secondary navigation, and any content blocks near the page end."
            )

        server_entry = None
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None

        if not isinstance(server_entry, dict):
            return ToolResult(
                result_type="error",
                content=f"playwright_page_overview error: MCP server entry missing for {self.SERVER_ID}",
                data={"server_id": self.SERVER_ID},
            )

        dom_ready = None
        original_viewport = None
        if resize_viewport:
            original_viewport = self._get_viewport(server_entry)
            if isinstance(original_viewport, dict):
                self._resize_viewport(server_entry, width=1400, height=1600)
        if wait_for_dom_ready:
            dom_ready = self._wait_for_dom_ready(server_entry, timeout_s=wait_seconds, poll_s=0.25)
        elif wait_seconds > 0:
            try:
                time.sleep(wait_seconds)
            except Exception:
                pass

        # Ensure we're at the top before the first capture.
        top_err = self._run_code(server_entry, "window.scrollTo(0, 0)")

        top_result = self._run_visual_scout(top_question)

        # Scroll to bottom and capture.
        bottom_err = self._run_code(
            server_entry, "window.scrollBy(0, window.innerHeight)"
        )
        if scroll_pause_seconds > 0:
            try:
                time.sleep(scroll_pause_seconds)
            except Exception:
                pass

        bottom_result = self._run_visual_scout(bottom_question)

        # Return to top so the planner is in a predictable state.
        back_err = self._run_code(server_entry, "window.scrollTo(0, 0)")
        if scroll_pause_seconds > 0:
            try:
                time.sleep(scroll_pause_seconds)
            except Exception:
                pass
        if resize_viewport and isinstance(original_viewport, dict):
            try:
                self._resize_viewport(
                    server_entry,
                    width=int(original_viewport.get("width") or 0),
                    height=int(original_viewport.get("height") or 0),
                )
            except Exception as e:
                logger.debug("Failed to restore original viewport after page overview: %s", e, exc_info=True)

        report_lines = [
            "Top of page:",
            self._format_scout_summary(top_result, default_note="(top scan failed)"),
            "Bottom of page:",
            self._format_scout_summary(bottom_result, default_note="(bottom scan failed)"),
        ]

        errors = [e for e in (top_err, bottom_err, back_err) if isinstance(e, str) and e.strip()]
        if errors:
            report_lines.append("Scroll/positioning notes:")
            for e in errors:
                report_lines.append(f"- {e}")

        return ToolResult(
            result_type="page_overview",
            content="\n".join([line for line in report_lines if line is not None]).strip(),
            data={
                "wait_seconds": wait_seconds,
                "wait_for_dom_ready": wait_for_dom_ready,
                "dom_ready": dom_ready,
                "scroll_pause_seconds": scroll_pause_seconds,
                "resize_viewport": resize_viewport,
                "top": self._extract_scout_payload(top_result),
                "bottom": self._extract_scout_payload(bottom_result),
            },
        )

    def _run_visual_scout(self, question: str) -> ToolResult:
        scout = WebVisualScout()
        msg = ToolMessage(
            tool_name="web_visual_scout",
            tool_data={
                "arguments": {
                    "question": question,
                    "full_page": False,
                }
            },
        )
        return scout.execute(msg)

    def _extract_scout_payload(self, res: ToolResult | None) -> dict[str, Any]:
        if not isinstance(res, ToolResult):
            return {"error": "no_result"}
        if res.result_type == "error":
            return {"error": res.content}
        if isinstance(res.data, dict):
            return res.data
        return {"raw": res.content}

    def _format_scout_summary(self, res: ToolResult | None, default_note: str) -> str:
        if not isinstance(res, ToolResult):
            return default_note
        if res.result_type == "error":
            return f"(error) {res.content}"
        if not isinstance(res.data, dict):
            return res.content or default_note
        scout = res.data.get("scout") if isinstance(res.data.get("scout"), dict) else {}
        overview = scout.get("page_overview") if isinstance(scout, dict) else None
        if isinstance(overview, str) and overview.strip():
            return overview.strip()
        return default_note

    def _wrap_page_eval(self, expr: str) -> str:
        # browser_evaluate runs the function in page (DOM) context — no
        # Playwright `page` object is available. The wrapper is just `() => { return EXPR; }`.
        return (
            "() => {\n"
            f"  return ({expr});\n"
            "}"
        )

    def _run_code(self, server_entry: dict, code: str) -> Optional[str]:
        try:
            call_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_EVALUATE,
                arguments={"function": self._wrap_page_eval(code)},
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
            text, is_error, _attachments = format_mcp_tool_result_content(call_resp)
            if is_error:
                return f"browser_evaluate error: {text or 'unknown error'}"
            return None
        except Exception as e:
            return f"browser_evaluate error: {e}"

    def _run_code_raw(self, server_entry: dict, code: str) -> tuple[Optional[str], Optional[str]]:
        try:
            call_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_EVALUATE,
                arguments={"function": self._wrap_page_eval(code)},
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
            text, is_error, _attachments = format_mcp_tool_result_content(call_resp)
            if is_error:
                return None, f"browser_evaluate error: {text or 'unknown error'}"
            return text, None
        except Exception as e:
            return None, f"browser_evaluate error: {e}"

    def _parse_jsonish(self, raw: str | None) -> Optional[dict]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        s = raw.strip()
        try:
            return json.loads(s)
        except Exception:
            return None

    def _get_viewport(self, server_entry: dict) -> Optional[dict]:
        text, err = self._run_code_raw(
            server_entry,
            "({width: window.innerWidth, height: window.innerHeight})",
        )
        if err:
            return None
        payload = self._parse_jsonish(text)
        if isinstance(payload, dict) and isinstance(payload.get("width"), (int, float)):
            return payload
        return None

    def _resize_viewport(self, server_entry: dict, width: int, height: int) -> None:
        if not width or not height:
            return
        try:
            call_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name="browser_resize",
                arguments={"width": int(width), "height": int(height)},
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
            _text, is_error, _attachments = format_mcp_tool_result_content(call_resp)
            if is_error:
                logger.warning("browser_resize returned error: %s", _text or "unknown error")
        except Exception as e:
            logger.warning("browser_resize failed: %s", e)

    def _wait_for_dom_ready(self, server_entry: dict, timeout_s: float, poll_s: float) -> bool:
        deadline = time.time() + max(0.0, float(timeout_s))
        while time.time() <= deadline:
            text, err = self._run_code_raw(server_entry, "document.readyState")
            if err:
                return False
            if isinstance(text, str):
                state = text.strip().lower()
                if "complete" in state or "interactive" in state:
                    return True
            try:
                time.sleep(poll_s)
            except Exception:
                break
        return False


def get_tool_class():
    return PlaywrightPageOverview
