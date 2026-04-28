from __future__ import annotations

import json
import re
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.lib.tools.playwright_snapshot_utils import (
    make_snapshot_id,
    summarize_actionable_snapshot,
    _LINE_WITH_REF_RE,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

# Query all interactive elements inside [role="dialog"].
# Uses querySelectorAll which finds off-screen DOM elements (no scrolling needed).
# For virtualized lists, scrolls the container via scrollTop and re-collects.
_JS_MODAL_ELEMENTS = """
async (page) => {
  return await page.evaluate(() => {
    const norm = (s, n = 140) => {
      const v = String(s || "").replace(/\\s+/g, " ").trim();
      return v.length > n ? v.slice(0, n) : v;
    };

    const GENERIC_LABELS = ["increase quantity", "decrease quantity", "+", "-", "add", "remove"];
    const isGenericLabel = (t) => {
      const tl = t.toLowerCase();
      return GENERIC_LABELS.some(g => tl.includes(g));
    };

    // Find the item name associated with a generic control (e.g. "Increase quantity by 1").
    // Walks up the DOM to find a row container, then extracts the first
    // text-only element that isn't itself an interactive control.
    const contextFor = (el) => {
      const INTERACTIVE_TAGS = new Set(["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA"]);
      // Walk up to find the row (a container with both text and the control)
      let row = el.parentElement;
      for (let i = 0; i < 6; i++) {
        if (!row || !row.parentElement) break;
        const rowText = (row.innerText || row.textContent || "").trim();
        // Stop when the row text is substantial but not too large
        if (rowText.length > 5 && rowText.length < 200) break;
        row = row.parentElement;
      }
      if (!row) return "";
      // Find the first text node/element in the row that isn't a control
      const walker = document.createTreeWalker(row, NodeFilter.SHOW_ELEMENT, null);
      let node = walker.nextNode();
      while (node) {
        if (!INTERACTIVE_TAGS.has(node.tagName) && !node.querySelector("button, a, input")) {
          const t = (node.innerText || node.textContent || "").trim();
          if (t.length > 2 && t.length < 100 && !isGenericLabel(t)) {
            return norm(t, 80);
          }
        }
        node = walker.nextNode();
      }
      return "";
    };

    const labelFor = (el) => {
      let t = norm(el.getAttribute && el.getAttribute("aria-label"));
      if (t) return t;
      t = norm(el.innerText || el.textContent);
      if (t) return t;
      const label = el.closest && el.closest("label");
      if (label) {
        t = norm(label.innerText || label.textContent);
        if (t) return t;
      }
      // For small buttons (+, -, x) look at the parent row for context text
      const parent = el.parentElement;
      if (parent) {
        t = norm(parent.innerText || parent.textContent);
        if (t) return t;
      }
      // Try grandparent for deeper nesting (e.g. button inside a wrapper inside a row)
      const gp = parent && parent.parentElement;
      if (gp) {
        const gpText = norm(gp.innerText || gp.textContent);
        if (gpText && gpText.length < 100) return gpText;
      }
      t = norm(el.getAttribute && el.getAttribute("placeholder"));
      if (t) return t;
      t = norm(el.getAttribute && el.getAttribute("name"));
      if (t) return t;
      return "";
    };

    const roleFor = (el) => {
      const explicit = el.getAttribute && el.getAttribute("role");
      if (explicit) return explicit.toLowerCase();
      const tag = (el.tagName || "").toLowerCase();
      const type = ((el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
      if (tag === "button") return "button";
      if (tag === "a") return "link";
      if (tag === "select") return "option";
      if (tag === "textarea") return "textarea";
      if (tag === "input" && type === "radio") return "radio";
      if (tag === "input" && type === "checkbox") return "checkbox";
      if (tag === "input") return "input";
      return "unknown";
    };

    // Find the modal container
    let modal = document.querySelector('[role="dialog"]');
    if (!modal) modal = document.querySelector('[aria-modal="true"]');
    if (!modal) return { error: "no_modal_found", elements: [] };

    const SELECTOR = [
      'button', 'a[href]', 'input', 'textarea', 'select',
      '[role="radio"]', '[role="checkbox"]', '[role="button"]',
      '[role="option"]', '[role="tab"]', '[role="menuitem"]',
      '[role="switch"]', '[role="combobox"]',
    ].join(", ");

    const collect = () => {
      const nodes = modal.querySelectorAll(SELECTOR);
      const out = [];
      for (const el of nodes) {
        if (out.length >= 200) break;
        const cs = window.getComputedStyle(el);
        // Skip truly hidden elements, but keep off-screen/scrolled-away ones
        if (cs.display === "none" || cs.visibility === "hidden") continue;
        if (el.disabled) continue;

        const role = roleFor(el);
        let text = labelFor(el);
        const checked = el.checked || el.getAttribute("aria-checked") === "true";

        // For generic-labeled controls, find the associated item name
        if (isGenericLabel(text)) {
          const ctx = contextFor(el);
          if (ctx) text = text + " (" + ctx + ")";
        }

        out.push({ role, text: text || "(no label)", checked: checked || false });
      }
      return out;
    };

    // First pass: collect all elements already in DOM (works for non-virtualized)
    let elements = collect();

    // Check for virtualization: if the modal has a scrollable container,
    // scroll it via scrollTop to force lazy-loaded items into the DOM.
    const scroller = modal.querySelector('[class*="scroll"], [class*="body"], [style*="overflow"]')
      || modal;
    const scrollable = scroller.scrollHeight > scroller.clientHeight + 50;

    if (scrollable) {
      const seen = new Set(elements.map(e => e.role + "|" + e.text));
      let lastTop = -1;
      for (let i = 0; i < 15; i++) {
        scroller.scrollTop += scroller.clientHeight * 0.8;
        // Small delay not possible in synchronous evaluate, but scrollTop is immediate
        const newItems = collect();
        for (const item of newItems) {
          const key = item.role + "|" + item.text;
          if (!seen.has(key)) {
            seen.add(key);
            elements.push(item);
          }
        }
        if (scroller.scrollTop === lastTop) break;
        lastTop = scroller.scrollTop;
      }
      // Scroll back to top
      scroller.scrollTop = 0;
    }

    return { error: null, elements: elements, scrollable: scrollable };
  });
}
""".strip()


def _parse_jsonish(text: str) -> Any:
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    # Strip markdown headers like "### Result\n"
    s = re.sub(r"^#+\s*\w+\s*\n", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(s.lstrip())
        return obj
    except Exception:
        pass
    m = re.search(r"```json\s*([\s\S]*?)```", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"```\s*([\s\S]*?)```", s)
    if m:
        try:
            return json.loads((m.group(1) or "").strip())
        except Exception:
            pass
    return None


def _match_refs_by_proximity(elements: list[dict], snapshot_text: str) -> None:
    """Enrich each element dict with a 'ref' key by finding it in the snapshot.

    For each element, search the snapshot for a line containing its label text,
    then pick the closest line with an actionable ref.
    """
    lines = (snapshot_text or "").splitlines()
    if not lines:
        return

    # Pre-parse all ref lines: [(line_idx, ref, role, label)]
    ref_lines: list[tuple[int, str, str, str]] = []
    for i, line in enumerate(lines):
        m = _LINE_WITH_REF_RE.match(line.rstrip())
        if m:
            ref = (m.group("ref") or "").strip()
            role = (m.group("role") or "").strip().lower()
            label = (m.group("label") or "").strip().lower()
            if ref:
                ref_lines.append((i, ref, role, label))

    used_refs: set[str] = set()

    for el in elements:
        label = (el.get("text") or "").strip()
        if not label or label == "(no label)":
            continue
        label_lower = label.lower()

        # Find the snapshot line that contains this label
        best_line_idx = None
        best_line_len = float("inf")
        for i, line in enumerate(lines):
            if label_lower in line.lower() and len(line) < best_line_len:
                best_line_idx = i
                best_line_len = len(line)

        if best_line_idx is None:
            continue

        # Find the closest ref line to this label line
        # Prefer: exact label match > closest by distance
        best_ref = None
        best_dist = float("inf")
        for (ri, ref, role, rlabel) in ref_lines:
            if ref in used_refs:
                continue
            dist = abs(ri - best_line_idx)
            if dist > 8:
                continue
            # Exact label match in the ref line gets priority
            if label_lower in rlabel:
                if dist < best_dist or best_ref is None:
                    best_ref = ref
                    best_dist = dist
            elif dist < best_dist:
                best_ref = ref
                best_dist = dist

        if best_ref:
            el["ref"] = best_ref
            used_refs.add(best_ref)


class WebModalScan(BaseTool):
    """
    Query the DOM for all interactive elements inside the open modal/dialog.
    Uses querySelectorAll (finds off-screen elements without scrolling).
    For virtualized lists, scrolls the container via scrollTop and re-collects.
    One MCP call, no mouse-position dependency.
    """

    SERVER_ID = "npm/playwright-mcp"

    def __init__(self):
        super().__init__("web_modal_scan")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_modal_scan error: MCP server missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        timeout = float(server_entry.get("policy", {}).get("call_timeout_seconds", 20))

        # One JS call to extract all interactive elements from the modal
        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name="browser_run_code",
            arguments={"code": _JS_MODAL_ELEMENTS},
            timeout_s=timeout,
        )
        text, is_error, _ = format_mcp_tool_result_content(call_resp)
        if is_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_modal_scan error: browser_run_code failed: {text}",
                abort_policy="abort_tool",
                retryable=True,
                details={"server_id": self.SERVER_ID},
            )

        raw_text = text if isinstance(text, str) else ""
        parsed = _parse_jsonish(raw_text)

        # Fallback: check structuredContent in the raw MCP response
        if not isinstance(parsed, dict) and isinstance(call_resp, dict):
            result_obj = call_resp.get("result") if isinstance(call_resp.get("result"), dict) else call_resp
            sc = result_obj.get("structuredContent") if isinstance(result_obj, dict) else None
            if isinstance(sc, dict):
                parsed = sc
            elif isinstance(sc, list):
                # Sometimes the result is wrapped in a list of content blocks
                for block in sc:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parsed = _parse_jsonish(str(block.get("text", "")))
                        if isinstance(parsed, dict):
                            break

        if not isinstance(parsed, dict):
            logger.error("web_modal_scan: could not parse JS result. Raw preview: %r", raw_text[:500])
            logger.error("web_modal_scan: call_resp type=%s keys=%s", type(call_resp).__name__,
                         list(call_resp.keys()) if isinstance(call_resp, dict) else "N/A")
            return make_tool_error(
                error_code="parse_error",
                message=f"web_modal_scan error: could not parse JS result. Preview: {raw_text[:200]}",
                abort_policy="abort_tool",
                retryable=True,
                details={"raw_preview": raw_text[:300]},
            )

        js_error = parsed.get("error")
        elements = parsed.get("elements") or []
        if js_error:
            return make_tool_error(
                error_code="no_modal",
                message=f"web_modal_scan: {js_error}. No modal/dialog found on the page.",
                abort_policy="abort_tool",
                retryable=True,
                details={"js_error": js_error},
            )

        # Take a snapshot to get refs
        snap_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name="browser_snapshot",
            arguments={},
            timeout_s=timeout,
        )
        snap_text, snap_err, _ = format_mcp_tool_result_content(snap_resp)
        snapshot_id = make_snapshot_id(self.name)
        snap_raw = snap_text if isinstance(snap_text, str) and not snap_err else ""
        final_summary = summarize_actionable_snapshot(snap_raw, max_elements=200)

        # Enrich elements with refs from the snapshot
        _match_refs_by_proximity(elements, snap_raw)

        # Group by role for readable output
        grouped: dict[str, list[dict]] = {}
        for el in elements:
            if not isinstance(el, dict):
                continue
            role = str(el.get("role") or "unknown").strip().lower()
            grouped.setdefault(role, []).append(el)

        lines = [
            f"web_modal_scan: {len(elements)} elements inside modal",
            "",
        ]
        for role in ["radio", "checkbox", "button", "input", "option", "link", "tab", "menuitem"]:
            items = grouped.pop(role, [])
            if items:
                lines.append(f"{role} ({len(items)}):")
                for el in items:
                    label = el.get("text", "(no label)")
                    checked = el.get("checked", False)
                    ref = el.get("ref", "")
                    mark = " [SELECTED]" if checked else ""
                    ref_str = f" [ref={ref}]" if ref else ""
                    lines.append(f"  - {label}{mark}{ref_str}")
                lines.append("")

        for role, items in grouped.items():
            if items:
                lines.append(f"{role} ({len(items)}):")
                for el in items:
                    label = el.get("text", "(no label)")
                    checked = el.get("checked", False)
                    ref = el.get("ref", "")
                    mark = " [SELECTED]" if checked else ""
                    ref_str = f" [ref={ref}]" if ref else ""
                    lines.append(f"  - {label}{mark}{ref_str}")
                lines.append("")

        content = "\n".join(lines)

        return ToolResult(
            result_type="web_modal_scan",
            content=content,
            data={
                "ok": True,
                "snapshot_id": snapshot_id,
                "snapshot_url": final_summary.get("url"),
                "snapshot_title": final_summary.get("title"),
                "all_elements": elements,
                "element_count": len(elements),
                "actionable_elements": elements,
                "actionable_total": len(elements),
            },
        )


def get_tool_class():
    return WebModalScan
