import json
from datetime import datetime
import re
from urllib.parse import urlsplit, parse_qsl
from typing import Dict, List, Optional, Tuple

from app.assistant.utils.pydantic_classes import ToolResult

from app.assistant.utils.time_utils import utc_to_local, convert_utc_object_to_local

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class DataConversionModule:
    def __init__(self):
        return

    # ---------------------------------------------------------------------
    # Playwright snapshot cleaning (deterministic, no LLM)
    # ---------------------------------------------------------------------
    @staticmethod
    def _clean_playwright_snapshot_for_history(
        snapshot_yaml: str,
        *,
        max_items: int = 40,
        # Default was ~2k; increased to keep more page context in history.
        max_chars: int = 10000,
    ) -> str:
        """
        Deterministically reduce a large Playwright accessibility snapshot to a compact,
        high-signal "interaction digest" suitable for prompt history (<~2k chars).

        Design goals:
        - Do NOT require an LLM.
        - Prefer actionable controls (buttons/links/textboxes/comboboxes/options).
        - Surface likely overlay/consent blockers first.
        - Stop early (streaming) once enough candidates are collected.

        Easy removal:
        - This function is only called from `_convert_mcp_playwright_tool_result`.
          Replacing those call sites with `_summarize_snapshot_refs` reverts behavior.
        """
        if not snapshot_yaml:
            return ""

        def _shorten_url(u: Optional[str], *, max_len: int = 120) -> Optional[str]:
            """
            For prompt history the agent usually only needs refs, not full URLs.
            DoorDash (and similar sites) often include extremely long cursor/query params.
            We keep only the path (drop query/fragment) to prevent multi-page history spam.
            If we cannot extract a reasonable path, return a generic placeholder.
            """
            if not u:
                return None
            u = str(u).strip()
            if not u:
                return None
            try:
                parts = urlsplit(u)
                path = parts.path or ""
                # For "URLs" that are actually just huge strings, path can be empty.
                if not path:
                    return "[url]"
                if len(path) <= max_len:
                    return path
                return path[: max_len - 3] + "..."
            except Exception:
                return "[url]"

        actionable_roles = {
            "link",
            "button",
            "textbox",
            "search",
            "menuitem",
            "checkbox",
            "radio",
            "combobox",
            "tab",
            "option",
        }
        input_roles = {"textbox", "combobox", "search"}
        consent_words = (
            "cookie",
            "consent",
            "accept",
            "reject",
            "agree",
            "continue",
            "close",
            "dismiss",
            "upgrade",
            "sign in",
            "sign up",
        )

        # Match snapshot lines like:
        # - button "See what's nearby" [ref=e45]
        # - link "Burger King" [ref=e1364]
        pat = re.compile(
            r'^\s*-\s*(?P<role>[a-zA-Z0-9_<>\-]+)'
            r'(?:\s+"(?P<label>[^"]{0,220})")?'
            r'(?P<rest>.*?\[ref=(?P<ref>[^\]]+)\].*)$'
        )

        pending_url: Optional[str] = None
        overlays: List[Tuple[int, str]] = []
        inputs: List[Tuple[int, str]] = []
        actions: List[Tuple[int, str]] = []

        def _mk_line(role: str, label: str, ref: str, url: Optional[str]) -> str:
            parts = [role]
            if label:
                safe_label = label.encode("ascii", "ignore").decode("ascii")
                if len(safe_label) > 120:
                    safe_label = safe_label[:117] + "..."
                parts.append(f"\"{safe_label}\"")
            parts.append(f"[ref={ref}]")
            if url and role == "link":
                short = _shorten_url(url)
                if short:
                    parts.append(f"url={short}")
            return " ".join(parts)

        # Stream scan: collect candidates and stop early.
        for raw_line in snapshot_yaml.splitlines():
            line = raw_line.rstrip()

            # Best-effort: attach /url to the next emitted link.
            murl = re.search(r"/url:\s*(\S+)", line)
            if murl:
                pending_url = murl.group(1).strip()
                continue

            m = pat.match(line)
            if not m:
                continue

            role = (m.group("role") or "").strip().lower()
            label = (m.group("label") or "").strip()
            ref = (m.group("ref") or "").strip()
            if not ref:
                continue

            label_l = label.lower()

            # Score: overlays/consent highest, then inputs, then general actions.
            score = 0
            if any(w in label_l for w in consent_words):
                score += 100
            if role in {"dialog", "alert", "banner"}:
                score += 70
            if role in input_roles:
                score += 40
            if role in actionable_roles:
                score += 10

            # Skip iframe noise unless it's clearly important.
            if role == "iframe" and score < 60:
                continue

            out_line = _mk_line(role=role, label=label, ref=ref, url=pending_url)
            if pending_url and role == "link":
                pending_url = None

            # Bucket
            if any(w in label_l for w in consent_words) or role in {"dialog", "alert", "banner"}:
                overlays.append((score, out_line))
            elif role in input_roles:
                inputs.append((score, out_line))
            elif role in actionable_roles:
                actions.append((score, out_line))

            # Early exit once we have enough overall candidates.
            if (len(overlays) + len(inputs) + len(actions)) >= max_items * 3:
                break

        def _take(items: List[Tuple[int, str]], n: int) -> List[str]:
            items.sort(key=lambda x: x[0], reverse=True)
            seen = set()
            out: List[str] = []
            for _s, v in items:
                if v in seen:
                    continue
                seen.add(v)
                out.append(v)
                if len(out) >= n:
                    break
            return out

        # Build digest with stable structure.
        # Keep overlays first, then inputs, then other actions.
        overlay_lines = _take(overlays, n=max(0, min(8, max_items)))
        input_lines = _take(inputs, n=max(0, min(10, max_items)))
        action_lines = _take(actions, n=max(0, min(max_items, 30)))

        lines: List[str] = []
        if overlay_lines:
            lines.append("Overlay/consent candidates:")
            lines.extend([f"- {l}" for l in overlay_lines])
        if input_lines:
            if lines:
                lines.append("")
            lines.append("Inputs:")
            lines.extend([f"- {l}" for l in input_lines])
        if action_lines:
            if lines:
                lines.append("")
            lines.append("Top actions:")
            lines.extend([f"- {l}" for l in action_lines])

        digest = "\n".join(lines).strip()
        if not digest:
            return ""

        if len(digest) > max_chars:
            digest = digest[: max_chars - 20].rstrip() + "\n\n[truncated]"
        return digest

    @staticmethod
    def convert(tool_result, level: str = "summary") -> Dict:
        """
        Converts tool result into a summarized form.
        Uses a mapping to route to the correct conversion function.
        Accepts ToolResult directly or a ToolMessage (unwraps its .tool_result).
        """
        from app.assistant.utils.pydantic_classes import ToolMessage as _TM
        if isinstance(tool_result, _TM):
            inner = getattr(tool_result, "tool_result", None)
            if isinstance(inner, ToolResult):
                tool_result = inner
            else:
                content = getattr(tool_result, "content", "") or str(tool_result)
                return {"tool_result": content}

        result_type = getattr(tool_result, "result_type", None)
        conversion_methods = {
            "fetch_email": DataConversionModule._convert_email,
            "get_email_messages": DataConversionModule._convert_get_email_messages,
            "search_result": DataConversionModule._convert_search,
            "send_email": DataConversionModule._convert_send_email,
            "search1": DataConversionModule._convert_search1,
            "scrape": DataConversionModule._convert_scrape,
            "calendar_events": DataConversionModule._convert_calendar,
            "scheduler_events": DataConversionModule._convert_scheduler_events,
            "todo_tasks": DataConversionModule._convert_todo_tasks,
            "weather": DataConversionModule._convert_weather,
            "news": DataConversionModule._convert_news,
            "final_answer": DataConversionModule._convert_final_answer,
            "node_description": DataConversionModule._convert_node_description,
            "search_web": DataConversionModule._convert_search_web,
            "web_page_coords": DataConversionModule._convert_web_page_coords,
            "web_spatial_snapshot": DataConversionModule._convert_web_spatial_snapshot,
            "web_visual_scout": DataConversionModule._convert_web_visual_scout,
            "web_navigate_snapshot": DataConversionModule._convert_compact_snapshot_tool,
            "web_navigate_back_snapshot": DataConversionModule._convert_compact_snapshot_tool,
            "web_click_ref_snapshot": DataConversionModule._convert_compact_snapshot_tool,
            "web_click_xy_snapshot": DataConversionModule._convert_compact_snapshot_tool,
            "web_fill_xy": DataConversionModule._convert_compact_snapshot_tool,
            "web_fill_ref": DataConversionModule._convert_compact_snapshot_tool,
            "web_type_focused": DataConversionModule._convert_compact_snapshot_tool,
            "web_scroll": DataConversionModule._convert_compact_snapshot_tool,
            "web_get_content": DataConversionModule._convert_generic_tool_result,
            "invoke_agent": DataConversionModule._convert_generic_tool_result,
            "save_daily_summary": DataConversionModule._convert_generic_tool_result,
            # Generic wrapper results (e.g., MCP tools return result_type="tool_result")
            "tool_result": DataConversionModule._convert_generic_tool_result,
            "error": DataConversionModule._convert_error,
            "ask_user_response": DataConversionModule._convert_ask_user_response,
            # Artifact reads: return content as-is so the planner can actually use it.
            "tool_result_artifact": DataConversionModule._convert_tool_result_artifact,
        }
        if result_type in conversion_methods:
            result = conversion_methods[result_type](tool_result, level)
        else:
            # Fallback: pass content through as plain text. If content looks
            # like JSON, convert to indented key-value for readability.
            content = str(getattr(tool_result, "content", None) or "No content")
            if content.strip().startswith("{") or content.strip().startswith("["):
                try:
                    parsed = json.loads(content)
                    from app.assistant.utils.prompt_formatter import format_for_prompt
                    content = format_for_prompt(parsed)
                except (json.JSONDecodeError, Exception):
                    pass
            result = {"tool_result": content}

        # Convert any UTC timestamps to local time in the final output.
        # Agent prompts should only see local times.
        return convert_utc_object_to_local(result)

    @staticmethod
    def _convert_web_page_coords(tool_result: ToolResult, level: str = "summary") -> Dict:
        """
        Compact summary for `web_page_coords` — only exposes click coordinates to the planner.
        Mark indices, image paths and overlay metadata are internal scaffolding the planner
        does not need and should not reason about.
        """
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        targets = data.get("targets")
        if not isinstance(targets, list):
            targets = []

        not_ready = bool(data.get("not_ready", False))
        if not_ready:
            return {"tool_result": "web_page_coords: page not ready — try again."}

        if not targets:
            requested = bool(data.get("requested_target_found", True))
            if not requested:
                return {"tool_result": "web_page_coords: target not found on page."}
            return {"tool_result": "web_page_coords: no click targets found."}

        lines = ["web_page_coords:"]
        for t in targets[:3]:
            if not isinstance(t, dict):
                continue
            x = t.get("x")
            y = t.get("y")
            label = t.get("label")
            label_s = f" ({label.strip()})" if isinstance(label, str) and label.strip() else ""
            lines.append(f"- click: x={x}, y={y}{label_s}")

        return {"tool_result": "\n".join(lines).strip()}

    @staticmethod
    def _convert_web_spatial_snapshot(tool_result: ToolResult, level: str = "summary") -> Dict:
        """
        Compact summary for `web_spatial_snapshot` so it can replace huge snapshots.
        """
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        url = meta.get("url") if isinstance(meta.get("url"), str) else None
        title = meta.get("title") if isinstance(meta.get("title"), str) else None
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        anchors = data.get("anchors")
        if not isinstance(anchors, list):
            anchors = []

        parts: List[str] = ["web_spatial_snapshot:"]
        if url:
            parts.append(f"- url: {url}")
        if title:
            parts.append(f"- title: {title}")

        try:
            at = int(stats.get("anchors_total")) if stats.get("anchors_total") is not None else None
        except Exception:
            at = None
        try:
            tc = int(stats.get("text_candidates")) if stats.get("text_candidates") is not None else None
        except Exception:
            tc = None

        parts.append(f"- anchors_returned: {len(anchors)}")
        if at is not None:
            parts.append(f"- anchors_total_seen: {at}")
        if tc is not None:
            parts.append(f"- text_candidates_scanned: {tc}")

        # Print only a small index of anchors; the agent can read_tool_result for full data if needed.
        if anchors:
            parts.append("- top_anchors:")
            for a in anchors[:12]:
                if not isinstance(a, dict):
                    continue
                aid = a.get("anchor_id")
                label = a.get("label") if isinstance(a.get("label"), str) else ""
                tag = a.get("tag") if isinstance(a.get("tag"), str) else ""
                role = a.get("role") if isinstance(a.get("role"), str) else ""
                cx = a.get("cx")
                cy = a.get("cy")
                score = a.get("score")
                nearby = a.get("nearby_text")
                if not isinstance(nearby, list):
                    nearby = []

                safe_label = label.strip().encode("ascii", "ignore").decode("ascii")
                if len(safe_label) > 90:
                    safe_label = safe_label[:87] + "..."
                rr = (role.strip() or tag or "anchor").strip()
                parts.append(f"  - id={aid} {rr} score={score} at=({cx},{cy}) label={safe_label!r}")
                if nearby:
                    # Show up to 2 snippets per anchor for locality context.
                    snips = []
                    for t in nearby[:2]:
                        if isinstance(t, str) and t.strip():
                            s = t.strip().encode("ascii", "ignore").decode("ascii")
                            if len(s) > 90:
                                s = s[:87] + "..."
                            snips.append(s)
                    if snips:
                        parts.append(f"    nearby: {snips}")
        else:
            parts.append("- top_anchors: []")

        return {"tool_result": "\n".join(parts).strip()}

    @staticmethod
    def _convert_web_visual_scout(tool_result: ToolResult, level: str = "summary") -> Dict:
        """
        Compact summary for `web_visual_scout` (prose-only vision scout).
        """
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        image_path = data.get("image_path") if isinstance(data.get("image_path"), str) else None
        scout = data.get("scout") if isinstance(data.get("scout"), dict) else {}
        overview = scout.get("page_overview") if isinstance(scout.get("page_overview"), str) else ""
        blockers = scout.get("blockers") if isinstance(scout.get("blockers"), list) else []

        parts: List[str] = ["web_visual_scout:"]
        if image_path:
            parts.append(f"- image_path: {image_path}")
        if blockers:
            parts.append(f"- blockers: {blockers[:5]}")
        if overview:
            parts.append("- page_overview:")
            parts.append(overview.strip())
        return {"tool_result": "\n".join(parts).strip()}

    @staticmethod
    def _convert_compact_snapshot_tool(tool_result: ToolResult, level: str = "summary") -> Dict:
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        result_type = str(tool_result.result_type or "snapshot_tool")
        # Keep tool-result history compact. Detailed refs live in the pinned
        # latest-snapshot state, not in generic tool-result history text.
        parts: List[str] = [f"{result_type}: snapshot_updated"]
        return {"tool_result": "\n".join(parts).strip()}

    @staticmethod
    def _convert_generic_tool_result(tool_result: ToolResult, level: str = "summary") -> Dict:
        """
        Compact summary for generic tool wrappers like MCP (result_type="tool_result").
        Keeps planner history small; full payload is stored as an artifact and referenced separately.
        """
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        if data.get("backend") == "mcp" and data.get("server_id") == "npm/playwright-mcp":
            return DataConversionModule._convert_mcp_playwright_tool_result(tool_result, level)

        # Keep full-ish tool results in planner history; only truncate extremely large payloads.
        content = (tool_result.content or "").strip()
        if len(content) > 50000:
            content = content[:50000] + "\n\n[truncated]"

        meta_bits = []
        backend = data.get("backend")
        if backend:
            meta_bits.append(f"backend={backend}")
        server_id = data.get("server_id")
        if server_id:
            meta_bits.append(f"server_id={server_id}")
        mcp_tool = data.get("mcp_tool_name")
        if mcp_tool:
            meta_bits.append(f"tool={mcp_tool}")
        attachments = data.get("attachments") if isinstance(data, dict) else None
        if isinstance(attachments, list) and attachments:
            meta_bits.append(f"attachments={len(attachments)}")

        header = "Tool returned"
        if meta_bits:
            header += f" ({', '.join(meta_bits)})"

        if not content:
            content = "[no content]"

        return {"tool_result": f"{header}:\n{content}"}

    @staticmethod
    def _convert_mcp_playwright_tool_result(tool_result: ToolResult, level: str = "summary") -> Dict:
        """
        Specialized, high-signal history trimming for Playwright MCP tool results.

        Goal:
        - Keep prompts small and actionable (URL/title + ref index)
        - Avoid iframe/ad-dominated snapshot dumps
        - Preserve full-fidelity via tool_result artifact + read_tool_result when needed
        """
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        mcp_tool = data.get("mcp_tool_name") or ""
        raw = (tool_result.content or "").strip()
        # Keep history ASCII-safe; non-ASCII in tool output is common (e.g., fancy quotes in site content).
        raw_ascii = raw.encode("ascii", "ignore").decode("ascii")

        url, title = DataConversionModule._extract_playwright_page_meta(raw_ascii)
        header_bits = ["backend=mcp", "server_id=npm/playwright-mcp"]
        if mcp_tool:
            header_bits.append(f"tool={mcp_tool}")
        header = f"Tool returned ({', '.join(header_bits)}):"

        # Screenshots: include the saved file path so the planner and final_answer
        # can reference the actual image and deliver it to the user.
        if mcp_tool == "browser_take_screenshot":
            parts = [header]
            if url:
                parts.append(f"- Page URL: {url}")
            if title:
                parts.append(f"- Page Title: {title}")
            att_list = data.get("attachments") if isinstance(data, dict) else None
            if isinstance(att_list, list):
                for att in att_list:
                    if not isinstance(att, dict) or att.get("type") != "image":
                        continue
                    path = att.get("path")
                    fname = att.get("original_filename")
                    if isinstance(path, str) and path.strip():
                        parts.append(f"Screenshot saved: {path.strip()}")
                    elif isinstance(fname, str) and fname.strip():
                        parts.append(f"Screenshot file: {fname.strip()}")
            if not any("Screenshot saved" in p or "Screenshot file" in p for p in parts):
                parts.append("Screenshot captured (no file path available).")
            return {"tool_result": "\n".join(parts)}

        # Snapshots: extract a compact "ref index" view.
        if mcp_tool == "browser_snapshot":
            snapshot = DataConversionModule._extract_markdown_code_block(raw_ascii, fence_lang="yaml")
            digest = DataConversionModule._clean_playwright_snapshot_for_history(snapshot or "")
            parts = [header]
            if url:
                parts.append(f"- Page URL: {url}")
            if title:
                parts.append(f"- Page Title: {title}")
            if digest:
                parts.append(digest)
            else:
                # Fallback: avoid dumping huge trees; keep a small preview.
                preview = raw_ascii[:50000] + ("\n\n[truncated]" if len(raw_ascii) > 50000 else "")
                parts.append(preview if preview else "[no content]")
            parts.append("Tip: use read_tool_result on [tool_result_id: ...] for full snapshot text.")
            return {"tool_result": "\n".join(parts)}

        # Click/navigate/wait/back/close often include a full snapshot block; prefer a small ref index instead.
        if mcp_tool in {
            "browser_navigate",
            "browser_click",
            "browser_wait_for",
            "browser_navigate_back",
            "browser_close",
        }:
            snapshot = DataConversionModule._extract_markdown_code_block(raw_ascii, fence_lang="yaml")
            digest = DataConversionModule._clean_playwright_snapshot_for_history(snapshot or "", max_items=25)
            parts = [header]
            if url:
                parts.append(f"- Page URL: {url}")
            if title:
                parts.append(f"- Page Title: {title}")
            if digest:
                parts.append(digest)
            parts.append("Tip: use read_tool_result on [tool_result_id: ...] for full details.")
            return {"tool_result": "\n".join(parts)}

        # Run-code and other tools: keep a small ASCII preview.
        preview = raw_ascii[:50000] + ("\n\n[truncated]" if len(raw_ascii) > 50000 else "")
        parts = [header]
        if url:
            parts.append(f"- Page URL: {url}")
        if title:
            parts.append(f"- Page Title: {title}")
        parts.append(preview if preview else "[no content]")
        return {"tool_result": "\n".join(parts)}

    @staticmethod
    def _extract_playwright_page_meta(text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract page URL/title from Playwright MCP markdown.
        Returns (url, title).
        """
        # IMPORTANT: some Playwright MCP tool outputs include multiple "### Page" sections
        # (e.g., after tab selection, retries, or embedded snapshots). We want the *latest*
        # URL/title, not the first one in the text blob.
        url: Optional[str] = None
        title: Optional[str] = None
        for line in (text or "").splitlines():
            s = (line or "").strip()
            if s.startswith("- Page URL:"):
                v = s.split(":", 2)[-1].strip()
                if v:
                    url = v
            elif s.startswith("- Page Title:"):
                v = s.split(":", 2)[-1].strip()
                if v:
                    title = v
        return url, title

    @staticmethod
    def _extract_markdown_code_block(text: str, fence_lang: str = "") -> Optional[str]:
        """
        Best-effort extraction of the first fenced markdown code block.
        If fence_lang is provided, matches ```<lang>.
        Returns inner text (no fences).
        """
        t = text or ""
        if fence_lang:
            m = re.search(rf"```{re.escape(fence_lang)}\s*\n([\s\S]*?)\n```", t)
        else:
            m = re.search(r"```\s*\n([\s\S]*?)\n```", t)
        if not m:
            return None
        return (m.group(1) or "").strip()

    @staticmethod
    def _summarize_snapshot_refs(snapshot_yaml: str, limit: int = 60) -> List[str]:
        """
        Reduce a Playwright accessibility snapshot (YAML-ish) to a compact list of actionable refs.
        """
        if not snapshot_yaml:
            return []

        def _shorten_url(u: Optional[str], *, max_len: int = 120) -> Optional[str]:
            if not u:
                return None
            u = str(u).strip()
            if not u:
                return None
            try:
                parts = urlsplit(u)
                path = parts.path or ""
                if not path:
                    return "[url]"
                if len(path) <= max_len:
                    return path
                return path[: max_len - 3] + "..."
            except Exception:
                return "[url]"

        actionable_roles = {
            "link",
            "button",
            "textbox",
            "search",
            "menuitem",
            "checkbox",
            "radio",
            "combobox",
            "tab",
            "option",
        }
        consent_words = (
            "cookie",
            "consent",
            "accept",
            "reject",
            "agree",
            "continue",
            "close",
            "dismiss",
            "upgrade",
            "sign in",
            "sign up",
        )

        items: List[Tuple[int, str]] = []

        # Match lines like:
        # - link "Text" [ref=e123] [cursor=pointer]:
        # Also handles non-quoted labels.
        pat = re.compile(
            r'^\s*-\s*(?P<role>[a-zA-Z0-9_<>\-]+)'
            r'(?:\s+"(?P<label>[^"]{0,200})")?'
            r'(?P<rest>.*?\[ref=(?P<ref>[^\]]+)\].*)$'
        )

        pending_url: Optional[str] = None
        for raw_line in snapshot_yaml.splitlines():
            line = raw_line.rstrip()

            # Capture /url lines to attach to the last link we emitted (best-effort).
            murl = re.search(r"/url:\s*(\S+)", line)
            if murl:
                pending_url = murl.group(1).strip()
                continue

            m = pat.match(line)
            if not m:
                continue

            role = (m.group("role") or "").strip().lower()
            label = (m.group("label") or "").strip()
            ref = (m.group("ref") or "").strip()

            # Score for ordering: consent/modals first, then actionable roles.
            score = 0
            label_l = label.lower()
            if any(w in label_l for w in consent_words):
                score += 100
            if role in actionable_roles:
                score += 10
            if role in {"dialog", "alert", "banner"}:
                score += 50

            # Skip obvious iframe noise unless it looks actionable.
            if role == "iframe" and score < 50:
                continue

            # Build a concise index line.
            parts = [role]
            if label:
                # Keep ASCII-safe label
                safe_label = label.encode("ascii", "ignore").decode("ascii")
                parts.append(f"\"{safe_label}\"")
            parts.append(f"[ref={ref}]")
            if pending_url and role == "link":
                short = _shorten_url(pending_url)
                if short:
                    parts.append(f"url={short}")
                pending_url = None

            items.append((score, " ".join(parts)))
            if len(items) >= limit * 3:
                break

        # Sort by score desc, then keep first N unique lines.
        items.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        out: List[str] = []
        for _score, line in items:
            if line in seen:
                continue
            seen.add(line)
            out.append(line)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _convert_tool_result_artifact(tool_result: ToolResult, level: str = "summary") -> Dict:
        """
        For read_tool_result outputs: keep the returned content (already truncated as requested).
        This is intentionally not aggressively summarized, because the whole point is to
        surface the full artifact content back into the planner context when needed.
        """
        content = (tool_result.content or "").strip()
        if not content:
            content = "[no content]"
        return {"tool_result": content}

    @staticmethod
    def _convert_email(tool_result: ToolResult, level: str, importance_threshold: int = 5) -> Dict:
        emails = tool_result.data_list
        formatted_emails = []

        for email in emails:
            importance = email.get("importance", 0)
            if importance < importance_threshold:
                continue  # Skip emails below threshold

            email_details = {
                "uid": email.get("uid", "No UID"),
                "subject": email.get("subject", "[No Subject]"),
                "sender": email.get("sender", "[Unknown Sender]"),
                "email_address": email.get("email_address", "[unknown@example.com]"),
                "date": email.get("date", "[No Date]"),
                "snippet": email.get("snippet", ""),
                "summary": email.get("summary", "[No Summary]"),
                "action_items": email.get("action_items", []),
                "importance": importance,
                "has_attachment": email.get("has_attachment", False),
            }
            if level == "full":
                email_details["body"] = email.get("body", "[No Content]")

            formatted_emails.append(email_details)

        if level == "full":
            return {"emails": formatted_emails}
        elif level == "summary":
            email_summaries = [
                {
                    "uid": e["uid"],
                    "subject": e["subject"],
                    "sender": e["sender"],
                    "date": e["date"],
                    "summary": e["summary"],
                }
                for e in formatted_emails
            ]
            return {"emails": email_summaries}
        elif level == "minimal":
            minimal_emails = [
                {"uid": e["uid"], "subject": e["subject"], "date": e["date"]}
                for e in formatted_emails
            ]
            return {"emails": minimal_emails}

        return {}

    @staticmethod
    def _convert_get_email_messages(tool_result: ToolResult, level: str) -> Dict:
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        if level in ("full", "summary"):
            return {
                "query": data.get("query"),
                "account_id": data.get("account_id"),
                "message_count": len(messages),
                "messages": messages,
            }
        if level == "minimal":
            return {"message_count": len(messages)}
        return {}




    @staticmethod
    def _convert_calendar(tool_result: ToolResult, level: str) -> Dict:
        events = tool_result.data_list

        logger.debug("Raw tool_result.data_list:")
        for event_obj in events:
            logger.debug(event_obj)  # Log the full event structure

        formatted_events = []

        for event_obj in events:
            event_id = event_obj.get("id", "No ID")
            title = event_obj.get("summary", "No Title")

            # Convert UTC to local time
            start_utc = event_obj.get("start")
            end_utc = event_obj.get("end")
            start_local = utc_to_local(start_utc).isoformat() if start_utc else None
            end_local = utc_to_local(end_utc).isoformat() if end_utc else None

            # Extract recurrence rule properly
            recurrence_list = event_obj.get("recurrence_rule", [])
            recurrence_rule = " ".join(recurrence_list) if recurrence_list else "Not a recurring event"

            # Build base event details
            event_details = {
                "event_id": event_id,
                "title": title,
                "start": start_local,  # Now in local time
                "end": end_local,  # Now in local time
                "recurrence_rule": recurrence_rule,
                "flexibility": event_obj.get("flexibility", "fixed"),
                "blocking": event_obj.get("blocking", True),
            }

            # Add extra fields for 'full' level
            if level == "full":
                event_details.update({
                    "description": event_obj.get("description", ""),
                    "attendees": event_obj.get("participants", []),
                    "link": event_obj.get("link"),
                })

            # Add extra fields for 'summary' only if they are non-empty
            elif level == "summary":
                if event_obj.get("description"):
                    event_details["description"] = event_obj["description"]
                if event_obj.get("participants"):
                    event_details["attendees"] = event_obj["participants"]

            formatted_events.append(event_details)

        # Build response with summary from tool_result.content
        result = {}
        if tool_result.content:
            result["summary"] = tool_result.content
        
        if level == "full":
            result["events"] = formatted_events
            return result
        elif level == "summary":
            result["events"] = formatted_events  # Includes only non-empty fields
            return result
        elif level == "minimal":
            minimal_events = [{"event_id": e["event_id"], "title": e["title"]} for e in formatted_events]
            result["events"] = minimal_events
            return result

        return {}


    @staticmethod
    def _convert_scheduler_events(tool_result: ToolResult, level: str) -> Dict:
        events = tool_result.data_list

        # Group events by original_event_id to identify duplicates
        grouped_events = {}
        for event in events:
            original_event_id = event.get("original_event_id", event.get("event_id", "No ID"))
            occurrence = event.get("occurrence") or event.get("event_payload", {}).get("occurrence")

            if occurrence:
                try:
                    occurrence_dt = datetime.fromisoformat(occurrence)
                except ValueError:
                    occurrence_dt = None

                if original_event_id not in grouped_events or \
                        (occurrence_dt and occurrence_dt < grouped_events[original_event_id]["occurrence_dt"]):
                    grouped_events[original_event_id] = {**event, "occurrence_dt": occurrence_dt}
            else:
                # No occurrence, assume it's a one-time event
                if original_event_id not in grouped_events:
                    grouped_events[original_event_id] = {**event, "occurrence_dt": None}

        # Format the collapsed events
        formatted_events = []
        summary_events = []
        minimal_events = []
        for event in grouped_events.values():
            original_event_id = event.get("original_event_id") or event.get("event_id", "No ID")
            event_id = original_event_id
            event_type = event.get("event_type", "unknown")
            start_date = event.get("start_date")
            end_date = event.get("end_date")
            occurrence = event.get("occurrence")
            interval = event.get("interval")
            payload = event.get("event_payload", {})
            title = event.get("title") or payload.get("title")
            task_type = event.get("task_type") or payload.get("task_type")
            importance = event.get("importance") or payload.get("importance")
            sound = event.get("sound") or payload.get("sound")
            event_payload = payload.get("payload_message", "")


            event_details = {
                "event_id": event_id,
                "title": title,
                "event_type": event_type,
                "start_date": start_date,
                "end_date": end_date,
                "occurrence": occurrence,
                "task_type": task_type,
                "interval": interval,
                "importance": importance,
                "sound": sound,
                "payload": event_payload,
            }
            formatted_events.append(event_details)

        # Return based on level of detail requested
        if level == "full":
            return {"events": formatted_events}

        elif level == "summary":
            for event in formatted_events:

                event_details = {
                    key: value for key, value in {
                        "event_id": event.get("event_id"),
                        "title": event.get("title"),
                        "event_type": event.get("event_type"),
                        "start_date": event.get("start_date"),
                        "end_date": event.get("end_date"),
                        "interval": f"{event.get('interval')} seconds",
                        "payload": event.get("payload"),
                    }.items() if value is not None
                }
                summary_events.append(event_details)
            return {"events": summary_events}


        elif level == "minimal":
            for event in formatted_events:
                allowed_keys = {"event_id", "title", "event_type", "start_date", "interval"}

                event_details = {
                    key: value for key, value in {
                        "event_id": event.get("event_id"),
                        "title": event.get("title"),
                        "event_type": event.get("event_type"),
                        "start_date": event.get("start_date"),
                    }.items() if value is not None and key in allowed_keys
                }
                minimal_events.append(event_details)
            return {"events": minimal_events}
        return {}


    @staticmethod
    def _convert_search(tool_result: ToolResult, level: str) -> Dict:
        results = tool_result.data_list
        links = getattr(tool_result, "links", {})
        if level == "full":
            return {"results": results, "links": links}
        elif level == "summary":
            top_link = list(links.keys())[0] if links else None
            return {"top_link": top_link}
        elif level == "minimal":
            return {"count": len(results)}
        return {}

    @staticmethod
    def _convert_send_email(tool_result: ToolResult, level: str) -> Dict:
        content = tool_result.content
        result = {
            "content": content,
        }
        if level == "full":
            return result
        elif level == "summary":
            return result
        elif level == "minimal":
            return result
        return {}

    @staticmethod
    def _convert_search1(tool_result: ToolResult, level: str) -> Dict:
        search_items = tool_result.data_list or []
        detailed_items = []
        for index, item in enumerate(search_items, start=1):
            summary_text = item.get("description", "No summary provided.")
            link = item.get("link", "No link provided.")
            item_details = {"description": summary_text, "link": link}
            if link != "No link provided.":
                item_details["link_details"] = f"Link: {link}"
            detailed_items.append(item_details)
        if level == "full":
            return {"results": detailed_items}
        elif level == "summary":
            return {"results": detailed_items}
        elif level == "minimal":
            return {"count": len(search_items)}
        return {}

    @staticmethod
    def _convert_scrape(tool_result: ToolResult, level: str) -> Dict:
        """
        Convert scrape tool results.

        Assumes:
        - tool_result.content is the extracted or full page text.
        - tool_result.data_list is a list of items, each with:
          - url: str
          - description: str
        """
        scraped_content = tool_result.content or ""
        raw_links = tool_result.data_list or []

        found_links = []
        for item in raw_links:
            # Support both dicts and objects with attributes
            if isinstance(item, dict):
                url_val = item.get("url")
                desc_val = item.get("description", "")
            else:
                url_val = getattr(item, "url", None)
                desc_val = getattr(item, "description", "") or ""

            if not url_val:
                continue

            found_links.append(
                {
                    "url": url_val,
                    "description": desc_val,
                }
            )

        if level == "full":
            return {
                "scraped_content": scraped_content,
                "links": found_links,
            }
        elif level == "summary":
            return {
                "scraped_content": scraped_content,
                "links": found_links,
            }
        elif level == "minimal":
            # Minimal view: just content, trimmed if you want to keep it short
            return {
                "content": scraped_content,
            }
        return {}

    @staticmethod
    def _convert_todo_tasks(tool_result: ToolResult, level: str) -> Dict:
        tasks = tool_result.data_list
        task_details = "TODO Tasks:\n"
        for idx, task in enumerate(tasks, start=1):
            try:
                task_id = task.get("id", "N/A")
                title = task.get("title", "Untitled")
                due = task.get("due")
                notes = task.get("notes", "")
                status = task.get("status", "No status")
                tasklist_name = task.get("tasklist_title", "Unknown")
                if due:
                    try:
                        due_datetime = datetime.fromisoformat(due.replace("Z", "+00:00"))
                        due_date = due_datetime.strftime("%Y-%m-%d")
                    except Exception:
                        due_date = "Invalid date"
                else:
                    due_date = "No due date"
                task_details += (
                    f"Task {idx}:\n"
                    f"  - ID: {task_id}\n"
                    f"  - Title: {title}\n"
                    f"  - Due Date: {due_date}\n"
                    f"  - Status: {status}\n"
                    f"  - Notes: {notes if notes else 'No notes'}\n"
                    f"  - Task List: {tasklist_name}\n"
                )
            except Exception as e:
                task_details += f"Error processing task {idx}: {e}\n"
        if level == "full":
            return {"tasks": tasks, "details": task_details}
        elif level == "summary":
            return {"details": task_details}
        elif level == "minimal":
            return {"count": len(tasks)}
        return {}

    @staticmethod
    def _convert_weather(tool_result: ToolResult, level: str) -> Dict:
        if level == "full":
            return {"weather": tool_result.content, "details": tool_result.data_list}
        elif level == "summary":
            return {"weather": tool_result.content}
        elif level == "minimal":
            return {"weather": tool_result.content}
        return {}

    @staticmethod
    def _convert_news(tool_result: ToolResult, level: str) -> Dict:
        if level == "full":
            return {"news": tool_result.content, "articles": tool_result.data_list}
        elif level == "summary":
            return {"news": tool_result.content}
        elif level == "minimal":
            return {"count": len(tool_result.data_list)}
        return {}

    @staticmethod
    def _convert_success(tool_result: ToolResult, level: str) -> Dict:
        content = tool_result.content
        if level in ("full", "summary"):
            return {"success": True, "content": content}
        elif level == "minimal":
            return {"success": True}
        return {}

    @staticmethod
    def _convert_error(tool_result: ToolResult, level: str) -> Dict:
        content = tool_result.content
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        error_code = data.get("error_code")
        abort_policy = data.get("abort_policy")
        retryable = data.get("retryable")
        user_visible = data.get("user_visible")
        details = data.get("details")
        if level in ("full", "summary"):
            payload = {"error": True, "content": content}
            if isinstance(error_code, str):
                payload["error_code"] = error_code
            if isinstance(abort_policy, str):
                payload["abort_policy"] = abort_policy
            if isinstance(retryable, bool):
                payload["retryable"] = retryable
            if isinstance(user_visible, bool):
                payload["user_visible"] = user_visible
            if isinstance(details, dict):
                payload["details"] = details
            return payload
        elif level == "minimal":
            return {"error": True, "error_code": error_code} if isinstance(error_code, str) else {"error": True}
        return {}

    @staticmethod
    def _convert_final_answer(tool_result: ToolResult, level: str) -> Dict:
        data_list = tool_result.data_list
        content = tool_result.content
        if data_list:
            from app.assistant.utils.prompt_formatter import format_for_prompt
            content += "\n" + format_for_prompt(data_list)
        if level in ("full", "summary"):
            return {"final_answer": content}
        elif level == "minimal":
            return {"final_answer": content}
        return {}

    @staticmethod
    def _convert_ask_user_response(tool_result: ToolResult, level: str) -> Dict:
        user_response = tool_result.content
        if level in ("full", "summary"):
            return {"user_response": f"User responded with: {user_response}"}
        elif level == "minimal":
            return {"user_response": user_response}
        return {}

    @staticmethod
    def _convert_tool_success(tool_result: ToolResult, level: str) -> Dict:
        content = tool_result.content
        if level in ("full", "summary"):
            return {"tool_success": content}
        elif level == "minimal":
            return {"tool_success": content[:5000]}
        return {}

    @staticmethod
    def _convert_tool_failure(tool_result: ToolResult, level: str) -> Dict:
        content = tool_result.content
        if level in ("full", "summary"):
            return {"tool_failure": content}
        elif level == "minimal":
            return {"tool_failure": content[:5000]}
        return {}

    @staticmethod
    def _convert_search_web(tool_result: ToolResult, level: str) -> Dict:
        """
        Convert search_web tool results into a formatted summary.
        Includes both the content from web_parser and any links.
        """
        content = tool_result.content or "No content returned"
        links = tool_result.data_list or []
        
        if level in ("full", "summary"):
            result = {"search_results": content}
            if links:
                result["sources"] = [
                    {"url": link.get('url', 'N/A'), "summary": link.get('summary', 'N/A')}
                    for link in links
                ]
            return result
        elif level == "minimal":
            return {"search_results": content[:2000] + "..." if len(content) > 2000 else content}
        return {}
    
    @staticmethod
    def _convert_node_description(tool_result: ToolResult, level: str) -> Dict:
        """
        Convert kg_describe_node results into a compact, agent-readable format.
        The tool already formats data as clean JSON, so just return it as-is.
        """
        if not tool_result.data:
            return {"node_description": "No node data available"}
        
        # Return the data as-is since kg_describe_node already formats it nicely
        return {"node_data": tool_result.data}
        
        # Old formatting code (kept for reference but not used)
        data = tool_result.data
        lines = []
        
        # Node header with core info
        lines.append(f"Node: {data['label']} ({data['node_type']})")
        lines.append(f"ID: {data['id']}")
        
        # Add warning if present
        if data.get('warning'):
            lines.append(f"WARNING: {data['warning']}")
        
        # Add description if present
        if data.get('description'):
            lines.append(f"Description: {data['description']}")
        
        # Add aliases if present
        if data.get('aliases'):
            aliases_str = ", ".join(data['aliases'])
            lines.append(f"Aliases: {aliases_str}")
        
        # Add temporal info if present
        if data.get('start_date') or data.get('end_date'):
            date_parts = []
            if data.get('start_date'):
                date_parts.append(f"from {data['start_date']}")
            if data.get('end_date'):
                date_parts.append(f"to {data['end_date']}")
            if data.get('start_date_confidence') or data.get('end_date_confidence'):
                conf_parts = []
                if data.get('start_date_confidence'):
                    conf_parts.append(f"start: {data['start_date_confidence']}")
                if data.get('end_date_confidence'):
                    conf_parts.append(f"end: {data['end_date_confidence']}")
                date_parts.append(f"({', '.join(conf_parts)})")
            lines.append(f"Dates: {' '.join(date_parts)}")
        
        # Add attributes if present and not empty
        if data.get('attributes') and data['attributes']:
            from app.assistant.utils.prompt_formatter import format_for_prompt
            lines.append(f"Attributes:\n{format_for_prompt(data['attributes'])}")
        
        # Add connections
        connections = []
        
        # Outbound connections (this node -> target node)
        for edge in data.get('outbound_edges', []):
            # New format uses target_node_label and relationship_type
            label = edge.get('target_node_label', edge.get('to_node_label', 'Unknown'))
            rel_type = edge.get('relationship_type', edge.get('edge_type', 'Unknown'))
            node_id = edge.get('target_node_id', edge.get('to_node_id', 'N/A'))
            
            conn = f"→ {label} ({rel_type})"
            if edge.get('sentence'):
                conn += f": \"{edge['sentence']}\""
            conn += f" [Node ID: {node_id}]"
            connections.append(conn)
        
        # Inbound connections (source node -> this node)
        for edge in data.get('inbound_edges', []):
            # New format uses source_node_label and relationship_type
            label = edge.get('source_node_label', edge.get('from_node_label', 'Unknown'))
            rel_type = edge.get('relationship_type', edge.get('edge_type', 'Unknown'))
            node_id = edge.get('source_node_id', edge.get('from_node_id', 'N/A'))
            
            conn = f"← {label} ({rel_type})"
            if edge.get('sentence'):
                conn += f": \"{edge['sentence']}\""
            conn += f" [Node ID: {node_id}]"
            connections.append(conn)
        
        if connections:
            lines.append("Connections:")
            lines.extend(f"  {conn}" for conn in connections)
        
        # Join all lines
        formatted_text = "\n".join(lines)
        
        if level in ("full", "summary"):
            return {"node_description": formatted_text}
        elif level == "minimal":
            # For minimal, just show core info and connection count
            minimal_lines = [
                f"Node: {data['label']} ({data['node_type']})",
                f"ID: {data['id']}"
            ]
            if data.get('description'):
                minimal_lines.append(f"Description: {data['description']}")
            
            total_connections = len(data.get('outbound_edges', [])) + len(data.get('inbound_edges', []))
            minimal_lines.append(f"Connections: {total_connections} total")
            
            return {"node_description": "\n".join(minimal_lines)}
        return {}

# ------------------------- Testing -------------------------
if __name__ == "__main__":
    # Sample calendar event data
    sample_calendar_result = ToolResult(
        result_type="calendar_events",
        data_list=[
            {
                "data": {
                    "summary": "Friday Night Meats",
                    "description": "Weekly Zoom meeting",
                    "htmlLink": "https://www.google.com/calendar/event?eid=some_id",
                    "start": {"dateTime": "2025-02-21T20:00:00-08:00"},
                    "end": {"dateTime": "2025-02-21T22:00:00-08:00"},
                    "attendees": [
                        {"email": "person1@example.com"},
                        {"email": "person2@example.com"},
                    ],
                    "id": "event123",
                }
            },
            {
                "data": {
                    "summary": "Kitchen Patrol",
                    "description": "Reminder for cleaning duties.",
                    "htmlLink": "https://www.google.com/calendar/event?eid=some_id_2",
                    "start": {"date": "2025-02-23"},
                    "end": {"date": "2025-02-24"},
                    "attendees": [{"email": "housemate@example.com"}],
                    "id": "event456",
                }
            },
        ],
    )

    print("\n==== Testing Calendar Conversion ====")
    print("\nFULL DETAIL:")
    print(json.dumps(DataConversionModule.convert(sample_calendar_result, "full"), indent=4))
    print("\nSUMMARY:")
    print(json.dumps(DataConversionModule.convert(sample_calendar_result, "summary"), indent=4))
    print("\nMINIMAL:")
    print(json.dumps(DataConversionModule.convert(sample_calendar_result, "minimal"), indent=4))

    # Sample email data
    sample_email_result = ToolResult(
        result_type="fetch_email",
        data_list=[
            {
                "uid": "12345",
                "subject": "Project Update",
                "sender": "Alice Johnson",
                "email_address": "alice@example.com",
                "date": "2025-02-20",
                "snippet": "Here's the latest update on the project...",
                "summary": "Key project updates and next steps.",
                "action_items": ["Follow up with Bob", "Update the report"],
                "importance": 8,
                "has_attachment": True,
                "body": "Dear team, Here’s the latest update on the project...",
            },
            {
                "uid": "67890",
                "subject": "Meeting Reminder",
                "sender": "Bob Smith",
                "email_address": "bob@example.com",
                "date": "2025-02-19",
                "snippet": "Reminder: Team meeting at 10 AM...",
                "summary": "Upcoming team meeting scheduled for 10 AM.",
                "action_items": ["Prepare agenda", "Confirm attendance"],
                "importance": 5,
                "has_attachment": False,
                "body": "Just a reminder that our team meeting is scheduled for tomorrow at 10 AM.",
            },
        ],
    )

    print("\n==== Testing Email Conversion ====")
    print("\nFULL DETAIL:")
    print(json.dumps(DataConversionModule.convert(sample_email_result, "full"), indent=4))
    print("\nSUMMARY:")
    print(json.dumps(DataConversionModule.convert(sample_email_result, "summary"), indent=4))
    print("\nMINIMAL:")
    print(json.dumps(DataConversionModule.convert(sample_email_result, "minimal"), indent=4))
