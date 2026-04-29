from __future__ import annotations

import json
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.lib.tools.playwright_dom_probe import probe_visible_dom_inputs
from app.assistant.lib.tools.playwright_snapshot_utils import (
    has_input_like_elements,
    make_snapshot_id,
    merge_actionable_with_dom_inputs,
    summarize_actionable_snapshot,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class WebFillRef(BaseTool):
    """
    Atomic "click-ref + clear + type + enter" for Playwright MCP.

    Same as web_fill_xy but uses a snapshot ref to focus the element instead
    of (x, y) coordinates. This is more reliable when the element is in the
    accessibility snapshot — no need to call web_page_coords first.

    Sequence:
      1. browser_click(ref) to focus the element
      2. browser_run_code: clear + type + optional Enter
      3. browser_snapshot for the post-action state
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_CLICK = "browser_click"
    MCP_RUN_CODE = "browser_run_code"
    MCP_SNAPSHOT = "browser_snapshot"

    def __init__(self):
        super().__init__("web_fill_ref")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        ref = str(args.get("ref") or "").strip()
        text = args.get("text")
        submit = bool(args.get("submit", True))
        clear_first = bool(args.get("clear_first", True))
        slowly = bool(args.get("slowly", False))
        capture_snapshot = bool(args.get("capture_snapshot", True))

        if not ref:
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_fill_ref error: `ref` is required.",
                abort_policy="abort_tool",
                retryable=False,
                details={"ref": ref},
            )
        if not isinstance(text, str) or not text.strip():
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_fill_ref error: missing required argument `text`.",
                abort_policy="abort_tool",
                retryable=False,
                details={"arguments": args},
            )

        # Look up the ref label from the latest snapshot for logging.
        element = ""
        try:
            snap = DI.global_blackboard.get_state_value("playwright_latest_snapshot")
            if isinstance(snap, dict):
                for el in (snap.get("actionable_elements") or []):
                    if isinstance(el, dict) and el.get("ref") == ref:
                        element = str(el.get("text") or "").strip()
                        break
        except Exception:
            pass

        server_entry = None
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_fill_ref error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        timeout_s = float(server_entry.get("policy", {}).get("call_timeout_seconds", 20))

        # Step 1: Click the ref to focus the element.
        # npm/playwright-mcp renamed `ref` to `target` (the field accepts a
        # snapshot ref OR a CSS selector). Cached MCP schema is the source.
        mcp_args = {"target": ref}
        if element:
            mcp_args["element"] = element
        click_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_CLICK,
            arguments=mcp_args,
            timeout_s=timeout_s,
        )
        click_text, click_error, _ = format_mcp_tool_result_content(click_resp)
        if click_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_fill_ref error: browser_click(ref={ref}) failed: {click_text}",
                abort_policy="abort_tool",
                retryable=True,
                details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_CLICK},
            )

        # Step 2: Clear + type + optional Enter via browser_run_code.
        t_json = json.dumps(text)
        js = f"""
async (page) => {{
  const TEXT = {t_json};
  const SUBMIT = {str(bool(submit)).lower()};
  const CLEAR_FIRST = {str(bool(clear_first)).lower()};
  const DELAY = {80 if slowly else 0};

  await page.waitForTimeout(60);

  if (CLEAR_FIRST) {{
    try {{
      await page.keyboard.press("Control+A");
      await page.keyboard.press("Backspace");
    }} catch (e) {{}}
    try {{
      await page.keyboard.press("Meta+A");
      await page.keyboard.press("Backspace");
    }} catch (e) {{}}
    try {{
      await page.evaluate(() => {{
        const el = document.activeElement;
        if (!el) return;
        const tag = (el.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea") {{
          el.value = "";
          el.dispatchEvent(new Event("input", {{ bubbles: true }}));
          el.dispatchEvent(new Event("change", {{ bubbles: true }}));
        }}
      }});
    }} catch (e) {{}}
  }}

  await page.keyboard.type(TEXT, DELAY ? {{ delay: DELAY }} : undefined);
  if (SUBMIT) {{
    await page.waitForTimeout(1000);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(220);
  }}

  return {{ ok: true }};
}}
""".strip()

        code_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_RUN_CODE,
            arguments={"code": js},
            timeout_s=timeout_s,
        )
        code_text, code_error, _ = format_mcp_tool_result_content(code_resp)
        if code_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_fill_ref error: browser_run_code (type) failed: {code_text}",
                abort_policy="abort_tool",
                retryable=True,
                details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_RUN_CODE},
            )

        # Step 3: Snapshot.
        parts = ["web_fill_ref:"]
        parts.append(f"- ref: {ref}")
        if element:
            parts.append(f"- element: {element}")
        parts.append(f"- text: {text[:120]!r}")
        parts.append(f"- submit: {bool(submit)}")
        parts.append(f"- clear_first: {bool(clear_first)}")
        parts.append("- status: submitted")

        result_data: dict[str, Any] = {
            "ok": True,
            "ref": ref,
            "element": element or None,
            "text": text,
            "submit": bool(submit),
            "clear_first": bool(clear_first),
            "slowly": bool(slowly),
        }

        if capture_snapshot:
            snap_text = self._capture_snapshot(server_entry)
            if not isinstance(snap_text, str) or not snap_text.strip():
                return make_tool_error(
                    error_code="snapshot_capture_failed",
                    message="web_fill_ref error: snapshot capture failed after action.",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_SNAPSHOT},
                )
            snapshot_id = make_snapshot_id(self.name)
            summary = summarize_actionable_snapshot(snap_text, max_elements=200)
            elements = summary.get("actionable_elements") if isinstance(summary.get("actionable_elements"), list) else []
            if not has_input_like_elements(elements):
                dom_inputs = probe_visible_dom_inputs(server_entry=server_entry, max_items=12)
                elements = merge_actionable_with_dom_inputs(elements=elements, dom_inputs=dom_inputs, max_elements=200)
            result_data["snapshot_id"] = snapshot_id
            result_data["snapshot_url"] = summary.get("url")
            result_data["snapshot_title"] = summary.get("title")
            result_data["actionable_elements"] = elements
            result_data["actionable_total"] = max(int(summary.get("actionable_total") or 0), len(elements))
            result_data["truncated"] = bool(summary.get("truncated", False))
            result_data["hidden_count"] = int(summary.get("hidden_count", 0) or 0)
            parts.append(f"- snapshot_id: {snapshot_id}")
            parts.append(f"- actionable_elements: {len(elements)}")

        return ToolResult(
            result_type="web_fill_ref",
            content="\n".join(parts).strip(),
            data=result_data,
        )

    def _capture_snapshot(self, server_entry: dict[str, Any]) -> str | None:
        try:
            call_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_SNAPSHOT,
                arguments={},
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
            text_out, is_error, _ = format_mcp_tool_result_content(call_resp)
            if is_error:
                logger.debug("web_fill_ref snapshot returned error: %s", text_out)
                return None
            return text_out if isinstance(text_out, str) and text_out.strip() else ""
        except Exception:
            logger.debug("web_fill_ref snapshot exception details", exc_info=True)
            return None


def get_tool_class():
    return WebFillRef
