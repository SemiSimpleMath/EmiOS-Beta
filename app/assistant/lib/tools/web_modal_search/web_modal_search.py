from __future__ import annotations

import json
import re
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
from app.assistant.lib.tools.playwright_snapshot_utils import make_snapshot_id, _LINE_WITH_REF_RE
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


# JS: find element containing query text inside [role="dialog"], scroll it into view.
# Returns the matched text so we can confirm what was found.
_JS_SEARCH_NEIGHBORHOOD = """
async (page, query) => {
  return await page.evaluate((q) => {
    const norm = (s, n = 140) => {
      const v = String(s || "").replace(/\\s+/g, " ").trim();
      return v.length > n ? v.slice(0, n) : v;
    };

    // Find the modal container
    let modal = document.querySelector('[role="dialog"]');
    if (!modal) modal = document.querySelector('[aria-modal="true"]');
    if (!modal) return { error: "no_modal_found" };

    // Fuzzy scoring: count how many query words appear in the text.
    // Returns 0..1 where 1 = all words matched.
    const qWords = q.toLowerCase().trim().split(/\\s+/).filter(w => w.length > 0);
    const fuzzyScore = (text) => {
      const tLower = text.toLowerCase();
      let hits = 0;
      for (const w of qWords) {
        if (tLower.includes(w)) hits++;
      }
      return qWords.length > 0 ? hits / qWords.length : 0;
    };

    // Walk all elements to find matches (score > 0, text < 300 chars).
    // Keep the tightest (shortest) text per unique text value.
    const walker = document.createTreeWalker(
      modal, NodeFilter.SHOW_ELEMENT, null
    );

    const candidates = [];  // { node, text, score }
    const seenTexts = new Set();

    let node = walker.nextNode();
    while (node) {
      const text = (node.innerText || node.textContent || "").trim();
      if (text.length > 0 && text.length < 300) {
        const score = fuzzyScore(text);
        if (score > 0) {
          // Dedupe by exact text — keep the tightest (shortest) element
          const key = text.toLowerCase();
          if (!seenTexts.has(key)) {
            seenTexts.add(key);
            candidates.push({ node, text, score });
          }
        }
      }
      node = walker.nextNode();
    }

    if (candidates.length === 0) return { error: "no_match", query: q };

    // Sort: highest score first, then shortest text (tightest match)
    candidates.sort((a, b) => {
      if (b.score !== a.score) return b.score - a.score;
      return a.text.length - b.text.length;
    });

    // Take top matches (up to 5) — all with score equal to the best,
    // plus any others above 0.5 threshold.
    // Skip candidates whose text is a superstring of an already-selected match
    // (e.g., "Caramel Syrup +$1.00" when "Caramel Syrup" is already matched).
    const bestScore = candidates[0].score;
    const topMatches = [];
    for (const c of candidates) {
      if (topMatches.length >= 5) break;
      if (c.score < 0.5 && c.score < bestScore) break;
      const cLower = c.text.toLowerCase();
      const isSubsumed = topMatches.some(m => {
        const mLower = m.text.toLowerCase();
        return cLower.includes(mLower) && cLower.length > mLower.length;
      });
      if (isSubsumed) continue;
      topMatches.push(c);
    }

    const INTERACTIVE = 'button, a[href], input, select, textarea, ' +
      '[role="radio"], [role="checkbox"], [role="button"], [role="switch"]';

    // For each match, find its row container and nearby controls
    const results = [];
    for (const match of topMatches) {
      // Walk UP to find the row container
      let row = match.node;
      for (let i = 0; i < 6; i++) {
        const parent = row.parentElement;
        if (!parent || !modal.contains(parent)) break;
        const interactives = parent.querySelectorAll(INTERACTIVE);
        // Stop if parent is too wide (too many controls or too much text)
        if (interactives.length > 4) break;
        const parentText = (parent.innerText || parent.textContent || "").trim();
        if (parentText.length > 200) break;
        row = parent;
        if (interactives.length >= 1 && row !== match.node) break;
      }

      // Collect interactive elements in the row
      const controls = row.querySelectorAll(INTERACTIVE);
      const nearby = [];
      for (const ctrl of controls) {
        if (nearby.length >= 8) break;
        const cs = window.getComputedStyle(ctrl);
        if (cs.display === "none" || cs.visibility === "hidden") continue;
        if (ctrl.disabled) continue;

        const role = ctrl.getAttribute("role")
          || (ctrl.tagName === "BUTTON" ? "button" : ctrl.tagName.toLowerCase());
        let label = norm(ctrl.getAttribute("aria-label"));
        if (!label) label = norm(ctrl.innerText || ctrl.textContent);
        if (!label) label = norm(ctrl.getAttribute("title") || ctrl.getAttribute("name"));

        nearby.push({
          role: role.toLowerCase(),
          text: label || "(no label)",
        });
      }

      results.push({
        matched_text: norm(match.text, 200),
        row_text: norm(row.innerText || row.textContent, 300),
        score: match.score,
        nearby: nearby,
      });
    }

    // Scroll the best match into view
    const best = topMatches[0].node;
    let scroller = null;
    let el = best.parentElement;
    while (el && modal.contains(el)) {
      const style = window.getComputedStyle(el);
      const ov = style.overflowY;
      if ((ov === "auto" || ov === "scroll") && el.scrollHeight > el.clientHeight + 20) {
        scroller = el;
        break;
      }
      el = el.parentElement;
    }
    if (scroller) {
      const elRect = best.getBoundingClientRect();
      const scrollerRect = scroller.getBoundingClientRect();
      const elTop = elRect.top - scrollerRect.top + scroller.scrollTop;
      scroller.scrollTop = Math.max(0, elTop - scroller.clientHeight / 2 + elRect.height / 2);
    }

    return { error: null, matches: results };
  }, query);
}
""".strip()


def _parse_jsonish(text: str) -> Any:
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
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


_ACTIONABLE_ROLES = {
    "button", "link", "input", "textbox", "textarea", "search", "searchbox",
    "checkbox", "radio", "combobox", "menuitem", "tab", "option", "switch",
}


def _find_nearby_refs(snapshot_text: str, match_text: str, radius: int = 10) -> list[dict]:
    """
    Find the match_text in the snapshot, then return interactive elements
    with refs within `radius` lines above/below.
    """
    lines = (snapshot_text or "").splitlines()
    match_lower = match_text.lower().strip()
    if not match_lower:
        return []

    # Find all line indices where the match text appears
    match_indices = []
    for i, line in enumerate(lines):
        if match_lower in line.lower():
            match_indices.append(i)

    if not match_indices:
        return []

    # Use the first occurrence (tightest — shortest line containing the text)
    best_idx = min(match_indices, key=lambda i: len(lines[i]))

    # Collect interactive refs within radius lines
    start = max(0, best_idx - radius)
    end = min(len(lines), best_idx + radius + 1)

    nearby: list[dict] = []
    for i in range(start, end):
        m = _LINE_WITH_REF_RE.match(lines[i].rstrip())
        if not m:
            continue
        role = (m.group("role") or "").strip().lower()
        if role not in _ACTIONABLE_ROLES:
            continue
        ref = (m.group("ref") or "").strip()
        label = (m.group("label") or "").strip()
        if not ref:
            continue
        nearby.append({
            "ref": ref,
            "role": role,
            "text": label or "(no label)",
            "distance": abs(i - best_idx),
        })

    # Sort by distance from the match
    nearby.sort(key=lambda x: x["distance"])
    return nearby


class WebModalSearch(BaseTool):
    """
    Search for a specific item inside an open modal by text.
    Scrolls the matching element into view and takes a snapshot
    so the planner gets refs clustered around the target.
    """

    SERVER_ID = "npm/playwright-mcp"

    def __init__(self):
        super().__init__("web_modal_search")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        query = (tool_message.tool_data.get("arguments") or {}).get("query", "").strip()
        if not query:
            return make_tool_error(
                error_code="missing_query",
                message="web_modal_search error: 'query' argument is required.",
                abort_policy="abort_tool",
                retryable=False,
                details={},
            )

        try:
            server_entry = DI.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        except Exception:
            server_entry = None
        if not isinstance(server_entry, dict):
            return make_tool_error(
                error_code="mcp_server_missing",
                message=f"web_modal_search error: MCP server missing for {self.SERVER_ID}",
                abort_policy="abort_tool",
                retryable=False,
                details={"server_id": self.SERVER_ID},
            )

        timeout = float(server_entry.get("policy", {}).get("call_timeout_seconds", 20))

        # Find the element, its row container, and nearby interactive elements
        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name="browser_run_code",
            arguments={"code": f'async (page) => {{ const fn = {_JS_SEARCH_NEIGHBORHOOD}; return await fn(page, {json.dumps(query)}); }}'},
            timeout_s=timeout,
        )
        text, is_error, _ = format_mcp_tool_result_content(call_resp)
        if is_error:
            return make_tool_error(
                error_code="mcp_call_failed",
                message=f"web_modal_search error: browser_run_code failed: {text}",
                abort_policy="abort_tool",
                retryable=True,
                details={"server_id": self.SERVER_ID},
            )

        raw_text = text if isinstance(text, str) else ""
        parsed = _parse_jsonish(raw_text)

        # Fallback: check structuredContent
        if not isinstance(parsed, dict) and isinstance(call_resp, dict):
            result_obj = call_resp.get("result") if isinstance(call_resp.get("result"), dict) else call_resp
            sc = result_obj.get("structuredContent") if isinstance(result_obj, dict) else None
            if isinstance(sc, dict):
                parsed = sc
            elif isinstance(sc, list):
                for block in sc:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parsed = _parse_jsonish(str(block.get("text", "")))
                        if isinstance(parsed, dict):
                            break

        if not isinstance(parsed, dict):
            logger.error("web_modal_search: could not parse JS result. Raw: %r", raw_text[:500])
            return make_tool_error(
                error_code="parse_error",
                message=f"web_modal_search error: could not parse JS result. Preview: {raw_text[:200]}",
                abort_policy="abort_tool",
                retryable=True,
                details={"raw_preview": raw_text[:300]},
            )

        js_error = parsed.get("error")
        if js_error == "no_modal_found":
            return make_tool_error(
                error_code="no_modal",
                message="web_modal_search: No modal/dialog found on the page.",
                abort_policy="abort_tool",
                retryable=True,
                details={"js_error": js_error},
            )
        if js_error == "no_match":
            return make_tool_error(
                error_code="no_match",
                message=f"web_modal_search: No element matching '{query}' found in the modal.",
                abort_policy="abort_tool",
                retryable=True,
                details={"query": query, "js_error": js_error},
            )
        if js_error:
            return make_tool_error(
                error_code="js_error",
                message=f"web_modal_search: JS error: {js_error}",
                abort_policy="abort_tool",
                retryable=True,
                details={"js_error": js_error},
            )

        matches = parsed.get("matches") or []

        # Take a snapshot to get current refs
        snap_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name="browser_snapshot",
            arguments={},
            timeout_s=timeout,
        )
        snap_text, snap_err, _ = format_mcp_tool_result_content(snap_resp)
        snapshot_id = make_snapshot_id(self.name)
        snap_raw = snap_text if isinstance(snap_text, str) and not snap_err else ""

        # For each match, find nearby refs by proximity in the snapshot
        for match in matches:
            matched_text = match.get("matched_text", "")
            snap_nearby = _find_nearby_refs(snap_raw, matched_text, radius=8)
            match["nearby_refs"] = snap_nearby[:5]  # top 5 closest

        # Build concise output
        lines = [
            f'web_modal_search: {len(matches)} match(es) for "{query}"',
            "",
        ]
        for i, match in enumerate(matches):
            matched_text = match.get("matched_text", "")
            row_text = match.get("row_text", "")
            score = match.get("score", 0)
            nearby_refs = match.get("nearby_refs") or []
            lines.append(f"Match {i + 1}: {matched_text} (score: {score:.0%})")
            lines.append(f"  Row: {row_text}")
            if nearby_refs:
                lines.append("  Nearby refs:")
                for el in nearby_refs:
                    lines.append(f"    - {el['role']}: {el['text']} [ref={el['ref']}]")
            else:
                lines.append("  (no refs found nearby — use screenshot to locate)")
            lines.append("")

        lines.append("Use web_click_ref_snapshot with a ref above to interact.")
        content = "\n".join(lines)

        return ToolResult(
            result_type="web_modal_search",
            content=content,
            data={
                "ok": True,
                "match_found": len(matches) > 0,
                "match_count": len(matches),
                "query": query,
                "matches": matches,
                "snapshot_id": snapshot_id,
            },
        )


def get_tool_class():
    return WebModalSearch
