from __future__ import annotations

import json
import re
from typing import Any

from app.assistant.lib.mcp.tool_runner import format_mcp_tool_result_content, mcp_stdio_call_tool
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _parse_jsonish(text: str) -> Any:
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
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
            pass
    starts = [x.start() for x in re.finditer(r"[\{\[]", s)]
    for idx in starts:
        tail = s[idx:].lstrip()
        try:
            obj, _ = json.JSONDecoder().raw_decode(tail)
            return obj
        except Exception:
            continue
    logger.debug("playwright_dom_probe: all parse attempts failed for response preview=%r", s[:120])
    return None


def probe_visible_dom_inputs(*, server_entry: dict[str, Any], max_items: int = 12) -> list[dict[str, Any]]:
    """
    Probe visible, interactable input controls directly from DOM.
    """
    max_items = max(1, min(int(max_items or 12), 40))
    js = f"""
() => {{
  const MAX_ITEMS = {max_items};
  const norm = (s, n = 120) => {{
      const v = String(s || "").replace(/\\s+/g, " ").trim();
      return v.length > n ? v.slice(0, n) : v;
    }};
    const isVisible = (el) => {{
      if (!el) return false;
      const cs = window.getComputedStyle(el);
      if (!cs) return false;
      if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity || "1") < 0.05) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 6 || r.height < 6) return false;
      if (r.bottom <= 0 || r.right <= 0) return false;
      if (r.top >= window.innerHeight || r.left >= window.innerWidth) return false;
      return true;
    }};
    const labelFor = (el) => {{
      const aria = norm(el.getAttribute && el.getAttribute("aria-label"));
      if (aria) return aria;
      const ph = norm(el.getAttribute && el.getAttribute("placeholder"));
      if (ph) return ph;
      const id = norm(el.id);
      if (id) {{
        const byFor = document.querySelector(`label[for="${{CSS.escape(id)}}"]`);
        if (byFor) {{
          const t = norm(byFor.innerText || byFor.textContent);
          if (t) return t;
        }}
      }}
      const wrap = el.closest && el.closest("label");
      if (wrap) {{
        const t = norm(wrap.innerText || wrap.textContent);
        if (t) return t;
      }}
      const title = norm(el.getAttribute && el.getAttribute("title"));
      if (title) return title;
      const name = norm(el.getAttribute && el.getAttribute("name"));
      if (name) return name;
      const rid = norm(el.id);
      if (rid) return rid;
      return "";
    }};
    const nodes = Array.from(document.querySelectorAll("input, textarea, [role='textbox'], [role='searchbox'], [contenteditable='true']"));
    const out = [];
    for (const el of nodes) {{
      if (out.length >= MAX_ITEMS) break;
      if (!isVisible(el)) continue;
      const tag = String((el.tagName || "").toLowerCase());
      const type = String((el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
      const isTextarea = tag === "textarea";
      const disabled = !!el.disabled;
      const readonly = !!el.readOnly;
      if (disabled) continue;
      const role = isTextarea ? "textarea" : "input";
      const r = el.getBoundingClientRect();
      out.push({{
        ref: null,
        role,
        text: labelFor(el) || (isTextarea ? "textarea" : "input"),
        tag,
        type: type || null,
        x: Math.round(r.left + (r.width / 2)),
        y: Math.round(r.top + (r.height / 2)),
        width: Math.round(r.width),
        height: Math.round(r.height),
        disabled,
        readonly,
        source: "dom",
      }});
    }}
    return out;
}}
""".strip()
    call_resp = mcp_stdio_call_tool(
        server_entry=server_entry,
        tool_name="browser_evaluate",
        arguments={"function": js},
        timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
    )
    text, is_error, _attachments = format_mcp_tool_result_content(call_resp)
    if is_error:
        return []
    parsed = _parse_jsonish(text)
    if not isinstance(parsed, list):
        result = call_resp.get("result") if isinstance(call_resp, dict) else None
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        if isinstance(structured, list):
            parsed = structured
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").strip().lower()
        if role not in {"input", "textarea"}:
            continue
        out.append(row)
    return out

