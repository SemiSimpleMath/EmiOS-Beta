from __future__ import annotations

import json
import re
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


class WebFillXY(BaseTool):
    """
    Atomic "click + type + enter" for Playwright MCP.

    This prevents the planner from getting confused by the in-between state between
    focusing a textbox and typing. Everything happens in one Playwright `browser_run_code`
    call:
      - mouse click at (x,y)
      - clear (best-effort)
      - type text
      - optional Enter

    Use this when you already have reliable coordinates (usually from `web_page_coords`),
    especially for DoorDash address/search inputs.
    """

    SERVER_ID = "npm/playwright-mcp"
    # playwright-mcp 2026 upgrade: browser_run_code removed. We now compose
    # the click + type + submit sequence from native MCP tools because
    # browser_evaluate (the replacement) runs DOM-side and can't drive mouse
    # or keyboard. Trade: 3 MCP round-trips instead of 1 big browser_run_code.
    MCP_EVALUATE = "browser_evaluate"
    MCP_MOUSE_CLICK_XY = "browser_mouse_click_xy"
    MCP_PRESS_KEY = "browser_press_key"
    MCP_SNAPSHOT = "browser_snapshot"

    def __init__(self):
        super().__init__("web_fill_xy")

    @staticmethod
    def _extract_jsonish(text: str) -> Any:
        if not isinstance(text, str) or not text.strip():
            return None
        s = text.strip()

        # Common Playwright MCP wrapper format:
        # ### Result
        # { ...json... }
        # ### Ran Playwright code
        try:
            mres = re.search(r"^###\s*Result\s*\n([\s\S]*?)(?:\n###\s|$)", s, flags=re.IGNORECASE | re.MULTILINE)
            if mres:
                chunk = (mres.group(1) or "").strip()
                if chunk:
                    try:
                        return json.loads(chunk)
                    except Exception:
                        try:
                            obj, _idx = json.JSONDecoder().raw_decode(chunk.lstrip())
                            return obj
                        except Exception:
                            pass
        except Exception:
            pass

        try:
            return json.loads(s)
        except Exception:
            try:
                obj, _idx = json.JSONDecoder().raw_decode(s.lstrip())
                return obj
            except Exception:
                pass

        m = re.search(r"```json\s*([\s\S]*?)```", s, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"```\s*([\s\S]*?)```", s)
        if m:
            payload = (m.group(1) or "").strip()
            try:
                return json.loads(payload)
            except Exception:
                try:
                    obj, _idx = json.JSONDecoder().raw_decode(payload.lstrip())
                    return obj
                except Exception:
                    # Not JSON; keep searching (the first fenced block is often ```js```).
                    pass

        i1 = min([i for i in [s.find("{"), s.find("[")] if i >= 0] or [-1])
        if i1 >= 0:
            tail = s[i1:]
            try:
                obj, _idx = json.JSONDecoder().raw_decode(tail.lstrip())
                return obj
            except Exception:
                return None
        return None

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        x = args.get("x")
        y = args.get("y")
        text = args.get("text")
        submit = bool(args.get("submit", True))
        clear_first = bool(args.get("clear_first", True))
        slowly = bool(args.get("slowly", False))
        capture_snapshot = bool(args.get("capture_snapshot", True))

        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_fill_xy error: required numeric arguments `x` and `y` are missing",
                abort_policy="abort_tool",
                retryable=False,
                details={"arguments": args},
            )
        if not isinstance(text, str) or not text.strip():
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_fill_xy error: missing required argument `text`",
                abort_policy="abort_tool",
                retryable=False,
                details={"arguments": args},
            )

        server_entry = None
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_fill_xy error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        t_json = json.dumps(text)
        timeout_s = float(server_entry.get("policy", {}).get("call_timeout_seconds", 20))

        # Phase 1: click at (x, y) to focus the target via the native MCP tool.
        try:
            click_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_MOUSE_CLICK_XY,
                arguments={"x": float(x), "y": float(y), "element": f"target at ({x},{y})"},
                timeout_s=timeout_s,
            )
            _click_text, click_err, _ = format_mcp_tool_result_content(click_resp)
            if click_err:
                return make_tool_error(
                    error_code="mcp_call_failed",
                    message=f"web_fill_xy error: mouse click at ({x},{y}) failed: {_click_text}",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={"backend": "mcp", "mcp_tool_name": self.MCP_MOUSE_CLICK_XY},
                )
        except Exception as e:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_fill_xy error: mouse click at ({x},{y}) exception: {e}",
                abort_policy="abort_tool", retryable=True,
                details={"backend": "mcp", "mcp_tool_name": self.MCP_MOUSE_CLICK_XY},
            )

        # Phase 2: validate focused, optionally clear, set value, dispatch events.
        js = f"""
() => {{
  const TEXT = {t_json};
  const CLEAR_FIRST = {str(bool(clear_first)).lower()};

  const el = document.activeElement;
  if (!el) return {{ ok: false, reason: "no activeElement after click" }};
  const tag = (el.tagName || "").toLowerCase();
  const role = (el.getAttribute && el.getAttribute("role")) ? String(el.getAttribute("role")) : "";
  const ariaLabel = (el.getAttribute && el.getAttribute("aria-label")) ? String(el.getAttribute("aria-label")) : "";
  const placeholder = (el.getAttribute && el.getAttribute("placeholder")) ? String(el.getAttribute("placeholder")) : "";
  const id = (el.id) ? String(el.id) : "";
  const contentEditable = !!(el.isContentEditable);
  const isBody = (tag === "body" || tag === "html");
  const isInputLike = (tag === "input" || tag === "textarea" || tag === "select" || contentEditable || role === "textbox" || role === "combobox");
  const info = {{ ok: !isBody, tag, role, ariaLabel, placeholder, id, contentEditable, isInputLike, isBody }};

  if (isBody) {{
    return {{ ok: false, reason: "activeElement is not usable after click", active: info }};
  }}

  if (CLEAR_FIRST && (tag === "input" || tag === "textarea")) {{
    el.value = "";
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }}

  if (tag === "input" || tag === "textarea") {{
    el.value = TEXT;
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }} else if (contentEditable) {{
    el.textContent = TEXT;
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
  }} else {{
    return {{ ok: false, reason: "activeElement is not input/textarea/contentEditable", active: info }};
  }}

  const after = (tag === "input" || tag === "textarea")
    ? {{ value: String(el.value || "") }}
    : (contentEditable ? {{ value: String(el.innerText || "") }} : {{ value: null }});

  return {{ ok: true, x: {float(x)}, y: {float(y)}, active: info, clear_first: CLEAR_FIRST, after }};
}}
""".strip()

        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": js},
            timeout_s=timeout_s,
        )
        text_out, is_error, _attachments = format_mcp_tool_result_content(call_resp)
        if is_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_fill_xy error: MCP browser_evaluate returned isError: {text_out}",
                abort_policy="abort_tool",
                retryable=True,
                details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_EVALUATE},
            )

        # Phase 3: Enter-submit via native MCP tool if requested.
        if submit:
            try:
                press_resp = mcp_stdio_call_tool(
                    server_entry=server_entry,
                    tool_name=self.MCP_PRESS_KEY,
                    arguments={"key": "Enter"},
                    timeout_s=timeout_s,
                )
                _press_text, press_err, _ = format_mcp_tool_result_content(press_resp)
                if press_err:
                    logger.debug("[web_fill_xy] Enter press returned isError: %s", _press_text)
            except Exception as e:
                logger.debug("[web_fill_xy] Enter press exception (non-fatal): %s", e)

        parts = ["web_fill_xy:"]
        parts.append(f"- x: {float(x)}")
        parts.append(f"- y: {float(y)}")
        parts.append(f"- text: {text[:120]!r}")
        parts.append(f"- submit: {bool(submit)}")
        parts.append(f"- clear_first: {bool(clear_first)}")
        parts.append("- status: submitted")

        snapshot_text = None
        snapshot_id = None
        if capture_snapshot:
            snapshot_text = self._capture_snapshot(server_entry)
            if not isinstance(snapshot_text, str) or not snapshot_text.strip():
                return make_tool_error(
                    error_code="snapshot_capture_failed",
                    message="web_fill_xy error: snapshot capture failed after action.",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_SNAPSHOT},
                )
            snapshot_id = make_snapshot_id(self.name)

        if isinstance(snapshot_text, str):
            parts.append("- snapshot: captured")

        result_data: dict[str, Any] = {
            "ok": True,
            "x": float(x),
            "y": float(y),
            "text": text,
            "submit": bool(submit),
            "clear_first": bool(clear_first),
            "slowly": bool(slowly),
        }
        if isinstance(snapshot_text, str):
            summary = summarize_actionable_snapshot(snapshot_text, max_elements=200)
            elements = summary.get("actionable_elements") if isinstance(summary.get("actionable_elements"), list) else []
            if not has_input_like_elements(elements):
                dom_inputs = probe_visible_dom_inputs(server_entry=server_entry, max_items=12)
                elements = merge_actionable_with_dom_inputs(elements=elements, dom_inputs=dom_inputs, max_elements=200)
            result_data["snapshot_url"] = summary.get("url")
            result_data["snapshot_title"] = summary.get("title")
            result_data["actionable_elements"] = elements
            result_data["actionable_total"] = max(int(summary.get("actionable_total") or 0), len(elements))
            result_data["truncated"] = bool(summary.get("truncated", False))
            result_data["hidden_count"] = int(summary.get("hidden_count", 0) or 0)
            result_data["snapshot_id"] = snapshot_id
            parts.append(f"- snapshot_id: {snapshot_id}")
            parts.append(f"- actionable_elements: {len(elements)}")

        return ToolResult(
            result_type="web_fill_xy",
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
            text_out, is_error, _attachments = format_mcp_tool_result_content(call_resp)
            if is_error:
                logger.debug("web_fill_xy snapshot returned error: %s", text_out)
                return None
            return text_out if isinstance(text_out, str) and text_out.strip() else ""
        except Exception:
            logger.debug("web_fill_xy snapshot exception details", exc_info=True)
            return None


def get_tool_class():
    return WebFillXY

