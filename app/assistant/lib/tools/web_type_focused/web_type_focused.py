from __future__ import annotations

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


class WebTypeFocused(BaseTool):
    """
    Type into the currently focused element (no `ref` required).

    Why:
    - Playwright MCP `browser_type` requires an accessibility snapshot `ref`.
    - On JS-heavy sites (DoorDash), the planner often successfully focuses the right textbox
      but then loses the correct `ref` (or picks a non-input ref like an alert).
    - If the cursor is already in the correct input, typing should not require a ref.

    This tool uses `mcp::npm/playwright-mcp::browser_run_code` to type into `document.activeElement`
    using `page.keyboard.type()` and optionally presses Enter.
    """

    SERVER_ID = "npm/playwright-mcp"
    # playwright-mcp 2026 upgrade: browser_run_code → browser_evaluate.
    # Since browser_evaluate runs in DOM context (no `page` object), we can't
    # use page.keyboard.type. Instead set el.value + dispatch input/change
    # events; for the Enter-submit case, use browser_press_key separately.
    MCP_EVALUATE = "browser_evaluate"
    MCP_PRESS_KEY = "browser_press_key"
    MCP_SNAPSHOT = "browser_snapshot"

    def __init__(self):
        super().__init__("web_type_focused")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        text = args.get("text")
        submit = bool(args.get("submit", True))
        clear_first = bool(args.get("clear_first", True))
        slowly = bool(args.get("slowly", False))
        capture_snapshot = bool(args.get("capture_snapshot", True))

        if not isinstance(text, str) or not text.strip():
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_type_focused error: missing required argument `text`",
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
                message=f"web_type_focused error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        t_json = json.dumps(text)
        # Phase 1: validate focused element, optionally clear, set value, dispatch
        # input/change events. All DOM-side so browser_evaluate handles it.
        js = f"""
() => {{
  const TEXT = {t_json};
  const CLEAR_FIRST = {str(bool(clear_first)).lower()};

  const el = document.activeElement;
  if (!el) return {{ ok: false, reason: "no activeElement" }};
  const tag = (el.tagName || "").toLowerCase();
  const role = (el.getAttribute && el.getAttribute("role")) ? String(el.getAttribute("role")) : "";
  const ariaLabel = (el.getAttribute && el.getAttribute("aria-label")) ? String(el.getAttribute("aria-label")) : "";
  const id = (el.id) ? String(el.id) : "";
  const name = (el.getAttribute && el.getAttribute("name")) ? String(el.getAttribute("name")) : "";
  const contentEditable = !!(el.isContentEditable);
  const isBody = (tag === "body" || tag === "html");
  const isInputLike = (tag === "input" || tag === "textarea" || tag === "select" || contentEditable);
  const info = {{ ok: !isBody, tag, role, ariaLabel, id, name, contentEditable, isInputLike, isBody }};

  if (isBody) {{
    return {{ ok: false, reason: "activeElement is not a usable input", active: info }};
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

  return {{ ok: true, active: info, clear_first: CLEAR_FIRST }};
}}
""".strip()

        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": js},
            timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
        )
        text_out, is_error, _attachments = format_mcp_tool_result_content(call_resp)
        if is_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_type_focused error: MCP browser_evaluate returned isError: {text_out}",
                abort_policy="abort_tool",
                retryable=True,
                details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_EVALUATE},
            )

        # Phase 2: if submit was requested, fire Enter via browser_press_key.
        # We don't have page.waitForTimeout here; the agent should call
        # web_modal_scan after if it needs to wait for submit-handler effects.
        if submit:
            try:
                press_resp = mcp_stdio_call_tool(
                    server_entry=server_entry,
                    tool_name=self.MCP_PRESS_KEY,
                    arguments={"key": "Enter"},
                    timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
                )
                _press_text, press_err, _ = format_mcp_tool_result_content(press_resp)
                if press_err:
                    logger.debug("[web_type_focused] Enter press returned isError: %s", _press_text)
            except Exception as e:
                logger.debug("[web_type_focused] Enter press exception (non-fatal): %s", e)

        parts = ["web_type_focused:"]
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
                    message="web_type_focused error: snapshot capture failed after action.",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_SNAPSHOT},
                )
            snapshot_id = make_snapshot_id(self.name)

        if isinstance(snapshot_text, str):
            parts.append("- snapshot: captured")

        result_data: dict[str, Any] = {
            "ok": True,
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
            result_type="web_type_focused",
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
                logger.debug("web_type_focused snapshot returned error: %s", text_out)
                return None
            return text_out if isinstance(text_out, str) and text_out.strip() else ""
        except Exception:
            logger.debug("web_type_focused snapshot exception details", exc_info=True)
            return None


def get_tool_class():
    return WebTypeFocused

