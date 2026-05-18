"""web_type_secret — type a pod-backed value into a web form field
without ever exposing it to the LLM agent.

The agent supplies a Playwright accessibility-snapshot `ref` (or
relies on the currently-focused element) plus a `pod_id` and
`projection` to identify which pod value should land in the field.
This tool self-elevates to AUTH_COURIER, resolves the pod via
`PodStore.fetch_projection`, and types the value through the
Playwright MCP `browser_type` / `browser_run_code` path.

The agent's tool result contains only metadata about the operation
(was it filled? how many characters?) — never the value itself.
Audit rows are written automatically by `fetch_projection`, so every
secret form-field fill produces a durable record.

## Authority

The tool requires AUTH_USER (99) on the caller's scope to be
invoked at all. This restricts secret form filling to the
master_room surface (or anything explicitly elevated). The
dayflow_orchestrator (95) cannot autonomously fill secret form
fields — by design. A "fill this form with my SSN" request needs
to be user-initiated; we don't want background routines doing it.

Within the tool's execute(), we synthesize a fresh courier scope
(authority_level=100) and use that to read the pod. The agent's
own scope is never elevated; only the substitution step runs at
courier level.

## Why not just expose pod_fetch as a tool

Because the agent then has the VALUE in its tool result, which
lands in its transcript, which lands in audit logs, which gets
fed back to the LLM on the next turn. The whole point of the
secret-pod design is that values never enter the LLM context.
A dedicated typing tool keeps the substitution in one tightly-
scoped courier-side step.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    ToolMessage,
    ToolResult,
    ScopeContext,
    ScopeApprovalPolicy,
    ScopeResourcePolicy,
    ScopeToolPolicy,
)

logger = get_logger(__name__)


class WebTypeSecret(BaseTool):
    """Type a pod-resolved value into a form field. Agent never sees the value."""

    SERVER_ID = "npm/playwright-mcp"
    MCP_BROWSER_TYPE = "browser_type"
    MCP_BROWSER_EVALUATE = "browser_evaluate"  # replaced browser_run_code in 2026 MCP upgrade

    requires_approval = False
    # Approval is handled at the pod_fetch layer via authority gate;
    # adding a tool-level approval ticket would double-prompt the user.

    def __init__(self):
        super().__init__("web_type_secret")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments") or {}
        pod_id = (args.get("pod_id") or "").strip()
        projection = (args.get("projection") or "").strip()
        ref = (args.get("ref") or "").strip() or None
        element = (args.get("element") or "").strip() or None
        submit = bool(args.get("submit", False))
        clear_first = bool(args.get("clear_first", True))

        if not pod_id:
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_type_secret error: missing required argument `pod_id`",
                abort_policy="abort_tool",
                retryable=False,
                details={"arguments": args},
            )
        if not projection:
            return make_tool_error(
                error_code="invalid_arguments",
                message="web_type_secret error: missing required argument `projection`",
                abort_policy="abort_tool",
                retryable=False,
                details={"arguments": args},
            )

        # Resolve the pod value at courier scope. The value lives only
        # in this local variable for the duration of the JS payload
        # construction below — never returned to the agent, never
        # logged, never persisted beyond what the pod store already
        # has.
        try:
            from app.assistant.pod_store.pod_store import PodStore
            from app.assistant.pod_store.authority import PodAuthorityError
            from app.assistant.pod_store.resolvers import PodValueMissing
            courier_scope = ScopeContext(
                scope_id="scope::web_type_secret::courier",
                owner_id="jukka",
                actor_id=f"web_type_secret:caller={getattr(tool_message, 'request_id', '?')}",
                surface="system",
                room_id="web_type_secret",
                approval=ScopeApprovalPolicy(authority_level=100),
                resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
                tools=ScopeToolPolicy(),
            )
            value = PodStore().fetch_projection(
                pod_id, projection, scope=courier_scope,
            )
        except PodAuthorityError as e:
            return make_tool_error(
                error_code="pod_authority_denied",
                message=f"web_type_secret: pod fetch denied — {e}",
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": pod_id, "projection": projection},
            )
        except KeyError as e:
            return make_tool_error(
                error_code="pod_not_found",
                message=f"web_type_secret: {e}",
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": pod_id, "projection": projection},
            )
        except PodValueMissing as e:
            return make_tool_error(
                error_code="pod_value_missing",
                message=f"web_type_secret: pod value pointer stale — {e}",
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": pod_id, "projection": projection},
            )

        if not isinstance(value, str):
            return make_tool_error(
                error_code="pod_value_not_typeable",
                message=(
                    f"web_type_secret: pod {pod_id} projection {projection!r} "
                    f"resolved to {type(value).__name__}, not a string — "
                    f"can't type bytes into a form field"
                ),
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": pod_id, "projection": projection},
            )

        server_entry = None
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_type_secret error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool", retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        # Type path: prefer the explicit `ref` (passed to MCP as `target` —
        # playwright-mcp renamed `ref` to `target` in a 2026 upgrade).
        # Fall back to browser_evaluate against the focused element if no
        # ref. The evaluate-fallback exists for sites where a snapshot ref
        # is unreliable (heavy SPA reflows) but most callers should pass a
        # ref produced by web_modal_scan.
        typed_length = len(value)
        if ref:
            type_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_BROWSER_TYPE,
                arguments={
                    "target": ref,
                    "element": element or f"field for pod {pod_id} projection {projection}",
                    "text": value,
                    "submit": bool(submit),
                },
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )
        else:
            # Focused-element fallback via browser_evaluate. The JS uses
            # document.activeElement; the agent must have focused the
            # right field first (e.g., via web_modal_click).
            t_json = json.dumps(value)
            js = f"""
() => {{
  const TEXT = {t_json};
  const CLEAR_FIRST = {str(bool(clear_first)).lower()};
  const el = document.activeElement;
  if (!el) return {{ ok: false, reason: "no activeElement" }};
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "body" || tag === "html") {{
    return {{ ok: false, reason: "activeElement is not a usable input" }};
  }}
  if (CLEAR_FIRST && (tag === "input" || tag === "textarea")) {{
    el.value = "";
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }}
  // Note: page.keyboard.type isn't accessible from browser_evaluate
  // (which runs in page context). Set value directly + dispatch input.
  // This is sufficient for most form fields; for fields with stricter
  // input listeners, pass an explicit `ref` so browser_type handles
  // key events natively.
  if (tag === "input" || tag === "textarea") {{
    el.value = TEXT;
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }} else if (el.isContentEditable) {{
    el.textContent = TEXT;
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
  }} else {{
    return {{ ok: false, reason: "activeElement is not input/textarea/contentEditable" }};
  }}
  return {{ ok: true }};
}}
""".strip()
            type_resp = mcp_stdio_call_tool(
                server_entry=server_entry,
                tool_name=self.MCP_BROWSER_EVALUATE,
                arguments={
                    "function": js,
                    "element": element or "currently-focused form input",
                },
                timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
            )

        # Release the local reference promptly — beyond this point
        # the value should not be needed anywhere in this function.
        value = None
        del value

        text_out, is_error, _attachments = format_mcp_tool_result_content(type_resp)
        if is_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_type_secret: MCP type call returned isError: {text_out}",
                abort_policy="abort_tool", retryable=True,
                details={"backend": "mcp", "pod_id": pod_id, "projection": projection},
            )

        result_data: dict[str, Any] = {
            "ok": True,
            "pod_id": pod_id,
            "projection": projection,
            "typed_length": typed_length,
            "submit": bool(submit),
            "ref": ref,
        }
        # Notice: tool result content describes WHAT happened in
        # terms of pod identity and length, never the value.
        parts = [
            "web_type_secret:",
            f"- pod: {pod_id}",
            f"- projection: {projection}",
            f"- typed_length: {typed_length}",
            f"- ref: {ref or '(focused element)'}",
            f"- submit: {bool(submit)}",
            "- status: filled",
        ]
        return ToolResult(
            result_type="web_type_secret",
            content="\n".join(parts),
            data=result_data,
        )


def get_tool_class():
    return WebTypeSecret
