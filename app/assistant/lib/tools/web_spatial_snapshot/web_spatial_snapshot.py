from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.lib.tools.playwright_snapshot_utils import make_snapshot_id
from app.assistant.utils.logging_config import get_logger
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class WebSpatialSnapshot(BaseTool):
    """
    Spatially-pruned "page map" for Playwright MCP browsing.

    What it returns (no vision required):
    - A list of interactive anchors (buttons/links/inputs/etc) with rendered bounding boxes.
    - For each anchor, a small list of nearby visible text blocks (proximity filter).

    Why:
    - Raw DOM/accessibility snapshots can be huge and lose the 2D layout relationship between
      "this text" and "that button".
    - This tool asks the browser for rendered layout (getBoundingClientRect) and clusters content
      around interactable anchors to cut noise aggressively.
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_EVALUATE = "browser_evaluate"

    def __init__(self):
        super().__init__("web_spatial_snapshot")

    @staticmethod
    def _extract_jsonish(text: str) -> Any:
        """
        Playwright MCP `browser_evaluate` often returns markdown with fenced JSON.
        Extract + parse best-effort.
        """
        if not isinstance(text, str) or not text.strip():
            return None
        s = text.strip()

        # Fast path: raw JSON
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
                    pass

        # Last resort: try to locate the first '[' or '{' and parse from there.
        i1 = min([i for i in [s.find("["), s.find("{")] if i >= 0] or [-1])
        if i1 >= 0:
            tail = s[i1:]
            try:
                obj, _idx = json.JSONDecoder().raw_decode(tail.lstrip())
                return obj
            except Exception:
                return None
        return None

    def _mcp_call(self, *, server_entry: dict, tool_name: str, arguments: dict) -> tuple[str, bool, list[dict[str, Any]]]:
        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=tool_name,
            arguments=arguments or {},
            timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
        )
        return format_mcp_tool_result_content(call_resp)

    @staticmethod
    def _pick_first_text(content_items: list[dict[str, Any]] | None) -> str:
        for it in content_items or []:
            if isinstance(it, dict) and it.get("type") == "text" and isinstance(it.get("text"), str):
                return it["text"]
        return ""

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        question = (args.get("question") or "").strip()
        radius_px = int(args.get("radius_px", 160) or 160)
        max_anchors = int(args.get("max_anchors", 60) or 60)
        per_anchor_nearby = int(args.get("per_anchor_nearby", 4) or 4)
        max_text_candidates = int(args.get("max_text_candidates", 900) or 900)

        radius_px = max(40, min(radius_px, 800))
        max_anchors = max(10, min(max_anchors, 200))
        per_anchor_nearby = max(0, min(per_anchor_nearby, 12))
        max_text_candidates = max(100, min(max_text_candidates, 5000))

        # Resolve server entry via shared ToolRegistry instance
        server_entry = None
        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_spatial_snapshot error: MCP server entry missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                user_visible=False,
                details={"server_id": self.SERVER_ID},
            )

        # `browser_evaluate` runs JS in the page (DOM) context. document/window
        # are available directly; there is no Playwright `page` object.
        q_json = json.dumps(question or "")
        js = f"""
() => {{
  const q = {q_json};
  const RADIUS = {int(radius_px)};
  const MAX_ANCHORS = {int(max_anchors)};
  const MAX_TEXT = {int(max_text_candidates)};
  const PER_ANCHOR = {int(per_anchor_nearby)};
  function norm(s) {{
    return String(s || "").replace(/\\s+/g, " ").trim();
  }}
    function norm(s) {{
      return String(s || "").replace(/\\s+/g, " ").trim();
    }}
    const tokens = (norm(q).toLowerCase().match(/[a-z0-9]+/g) || []).slice(0, 12);
    const WANT = new Set(tokens);

    function isVisible(el) {{
      if (!el) return false;
      const st = window.getComputedStyle(el);
      if (!st) return false;
      if (st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 6 || r.height < 6) return false;
      if (r.bottom < 0 || r.right < 0) return false;
      if (r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    }}

    function elText(el) {{
      try {{
        const aria = norm(el.getAttribute("aria-label"));
        const title = norm(el.getAttribute("title"));
        const placeholder = norm(el.getAttribute("placeholder"));
        const value = norm(el.value);
        const alt = norm(el.getAttribute("alt"));
        const txt = norm(el.innerText || el.textContent);
        return (aria || title || placeholder || value || alt || txt || "").slice(0, 120);
      }} catch (e) {{
        return "";
      }}
    }}

    function scoreAnchor(el, label) {{
      const l = String(label || "").toLowerCase();
      let s = 0;
      // Overlay-ish actions
      if (/(close|dismiss|cancel|x\\b|\\u00d7)/.test(l)) s += 90;
      if (/(accept|agree|allow)/.test(l)) s += 80;
      if (/(reject|decline|no thanks)/.test(l)) s += 75;
      if (/(continue|next|confirm|save)/.test(l)) s += 55;
      // Query bias
      for (const t of WANT) {{
        if (t && l.includes(t)) s += 10;
      }}
      const tag = (el.tagName || "").toLowerCase();
      if (tag === "button") s += 25;
      if (tag === "a") s += 12;
      if (tag === "input" || tag === "textarea" || tag === "select") s += 18;
      const role = norm(el.getAttribute("role")).toLowerCase();
      if (role === "button" || role === "link") s += 10;
      return s;
    }}

    function center(r) {{
      return {{ cx: r.left + r.width / 2, cy: r.top + r.height / 2 }};
    }}
    function dist2(ax, ay, bx, by) {{
      const dx = ax - bx;
      const dy = ay - by;
      return dx * dx + dy * dy;
    }}

    // --- 1) anchors ---
    const anchorSelectors = [
      "button",
      "a[href]",
      "input",
      "textarea",
      "select",
      "[role='button']",
      "[role='link']",
      "[role='textbox']",
      "[contenteditable='true']",
      "[tabindex]"
    ];

    const anchorSeen = new Set();
    const anchorsRaw = [];
    for (const sel of anchorSelectors) {{
      for (const el of Array.from(document.querySelectorAll(sel))) {{
        if (!el || anchorSeen.has(el)) continue;
        anchorSeen.add(el);
        if (!isVisible(el)) continue;
        const r = el.getBoundingClientRect();
        const c = center(r);
        const label = elText(el);
        const id = norm(el.id);
        const role = norm(el.getAttribute("role"));
        const testid = norm(el.getAttribute("data-testid") || el.getAttribute("data-test") || el.getAttribute("data-qa"));
        const s = scoreAnchor(el, label);
        anchorsRaw.push({{
          tag: (el.tagName || "").toLowerCase(),
          role,
          label,
          id: id || null,
          testid: testid || null,
          x: r.left, y: r.top, w: r.width, h: r.height,
          cx: c.cx, cy: c.cy,
          score: s
        }});
      }}
    }}

    anchorsRaw.sort((a,b) => (b.score - a.score));
    const anchors = anchorsRaw.slice(0, MAX_ANCHORS).map((a, idx) => ({{ ...a, anchor_id: idx + 1 }}));

    // --- 2) text candidates ---
    const textSelectors = ["h1","h2","h3","p","span","li","div"];
    const textCands = [];
    for (const sel of textSelectors) {{
      for (const el of Array.from(document.querySelectorAll(sel))) {{
        if (textCands.length >= MAX_TEXT) break;
        if (!el || !isVisible(el)) continue;
        const t = norm(el.innerText || el.textContent);
        if (!t || t.length < 2) continue;
        // Skip very large blocks (nav/footer dumps); keep short snippets only.
        if (t.length > 220) continue;
        const r = el.getBoundingClientRect();
        const c = center(r);
        textCands.push({{ t: t.slice(0, 160), cx: c.cx, cy: c.cy }});
      }}
      if (textCands.length >= MAX_TEXT) break;
    }}

    const R2 = RADIUS * RADIUS;
    function nearbyForAnchor(a) {{
      if (PER_ANCHOR <= 0) return [];
      const out = [];
      const seen = new Set();
      for (const tc of textCands) {{
        if (out.length >= PER_ANCHOR) break;
        if (!tc || !tc.t) continue;
        const d2 = dist2(a.cx, a.cy, tc.cx, tc.cy);
        if (d2 > R2) continue;
        const k = tc.t;
        if (seen.has(k)) continue;
        seen.add(k);
        out.push(tc.t);
      }}
      return out;
    }}

    // Enrich anchors with nearby text.
    const enriched = anchors.map((a) => {{
      const nearby = nearbyForAnchor(a);
      // Add a small query bias: if question tokens appear in nearby text, bump score.
      let bias = 0;
      if (WANT.size) {{
        const joined = nearby.join(" ").toLowerCase();
        for (const t of WANT) {{
          if (t && joined.includes(t)) bias += 6;
        }}
      }}
      return {{ ...a, score: a.score + bias, nearby_text: nearby }};
    }});
    enriched.sort((a,b) => (b.score - a.score));

    return {{
      ok: true,
      meta: {{
        url: String(location.href || ""),
        title: String(document.title || ""),
        viewport: {{ w: window.innerWidth, h: window.innerHeight }},
      }},
      params: {{ radius_px: RADIUS, max_anchors: MAX_ANCHORS, per_anchor_nearby: PER_ANCHOR }},
      stats: {{ anchors_total: anchorsRaw.length, anchors_returned: enriched.length, text_candidates: textCands.length }},
      anchors: enriched
    }};
}}
""".strip()

        text, is_error, _attachments = self._mcp_call(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": js},
        )
        if is_error:
            return make_tool_error(
                error_code="mcp_browser_evaluate_error",
                message=f"web_spatial_snapshot error: MCP browser_evaluate returned isError: {text}",
                abort_policy="abort_tool",
                retryable=True,
                user_visible=False,
                details={"backend": "mcp", "server_id": self.SERVER_ID, "mcp_tool_name": self.MCP_EVALUATE},
            )

        payload = self._extract_jsonish(text)
        if not isinstance(payload, dict) or not payload.get("ok"):
            # Keep raw text around for debugging, but do not explode prompt history.
            preview = (text or "").strip()
            if len(preview) > 1200:
                preview = preview[:1200] + "\n\n[truncated]"
            return make_tool_error(
                error_code="mcp_json_parse_error",
                message="web_spatial_snapshot error: Could not parse browser_evaluate JSON payload.",
                abort_policy="abort_tool",
                retryable=True,
                user_visible=False,
                details={"raw_preview": preview},
            )

        anchors = payload.get("anchors")
        n_anchors = len(anchors) if isinstance(anchors, list) else 0

        # Provide a tiny content string; full details go in data.
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        url = meta.get("url") if isinstance(meta.get("url"), str) else None
        title = meta.get("title") if isinstance(meta.get("title"), str) else None

        parts = ["web_spatial_snapshot:"]
        if url:
            parts.append(f"- url: {url}")
        if title:
            parts.append(f"- title: {title}")
        snapshot_id = make_snapshot_id(self.name)
        parts.append(f"- snapshot_id: {snapshot_id}")
        parts.append(f"- anchors: {n_anchors}")
        payload["snapshot_id"] = snapshot_id

        return ToolResult(
            result_type="web_spatial_snapshot",
            content="\n".join(parts).strip(),
            data=payload,
        )


def get_tool_class():
    return WebSpatialSnapshot

