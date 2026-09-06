"""Playwright2ObservationNode — the standing perception of the v2 browser loop.

v1's planner perceives a one-line page identity; the actionable element list
only reaches it when a tool happens to return a snapshot, buried in growing
history — so the planner must CHOOSE to snapshot and often acts on a stale view.

v2 borrows the lesson from the leading open-source browser agent (Browser Use):
every loop iteration, a deterministic node reads the accessibility tree and
hands the planner a COMPACT, TASK-AGNOSTIC-BUT-FILTERED observation — only the
actionable elements, each with a stable [ref], role, and label — as a
first-class context item that is always current. On top of that it surfaces
SIGNALS (url changed, action failed, N new elements since last step), which is
the second Browser-Use idea: the planner reacts to what meaningfully changed
rather than re-reading the whole page.

The heavy lifting already exists in-repo: summarize_actionable_snapshot()
parses the tree into normalized actionable rows. This node runs it every step,
diffs against the previous observation, and writes:
  - playwright2_observation      (formatted text for the planner prompt)
  - playwright2_observation_data (the structured dict)
  - playwright2_signals          (formatted signal line)
It is signal-gated: a read-only last action on an unchanged URL reuses the
cached snapshot instead of paying for a fresh CDP round-trip.
"""
from __future__ import annotations

from typing import Any

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.mcp.tool_runner import format_mcp_tool_result_content, mcp_stdio_call_tool
from app.assistant.lib.tools.playwright_snapshot_utils import summarize_actionable_snapshot
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_last_tool_result_meta

logger = get_logger(__name__)

# Tools that do not mutate the page — after one of these, on an unchanged URL,
# the previous observation is still valid and we skip a fresh snapshot.
_READ_ONLY_TOOLS = {
    "web_get_content", "web_spatial_snapshot", "web_visual_scout",
    "playwright_page_overview", "web_page_coords", "web_modal_scan",
    "web_modal_search", "mcp::npm/playwright-mcp::browser_take_screenshot",
}
_MAX_ELEMENTS = 40


class Playwright2ObservationNode(ControlNode):
    SERVER_ID = "npm/playwright-mcp"
    MCP_SNAPSHOT = "browser_snapshot"

    def action_handler(self, message):  # noqa: ARG002
        self.blackboard.update_state_value("next_agent", None)
        try:
            self._observe()
        except Exception as e:
            # Never strand the loop on a perception hiccup — surface it to the
            # planner as an explicit signal instead of failing the manager.
            logger.error("[%s] observation failed: %s", self.name, e, exc_info=True)
            self.blackboard.update_state_value(
                "playwright2_observation",
                "OBSERVATION UNAVAILABLE this step (the page could not be read). "
                "Take a snapshot action or navigate, then look again.")
            self.blackboard.update_state_value("playwright2_signals", "observation error")
        self.blackboard.update_state_value("last_agent", self.name)

    # ------------------------------------------------------------------ #

    def _observe(self) -> None:
        last_meta = get_last_tool_result_meta(self.blackboard) or {}
        last_tool = str(last_meta.get("tool_name") or "").strip()
        last_error = bool(last_meta.get("is_error"))

        prev = self.blackboard.get_state_value("playwright2_observation_data", None)
        prev_url = str((prev or {}).get("url") or "") if isinstance(prev, dict) else ""
        prev_refs = set((prev or {}).get("_refs") or []) if isinstance(prev, dict) else set()

        server_entry = self.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        if not isinstance(server_entry, dict):
            raise RuntimeError(f"missing MCP server entry for {self.SERVER_ID}")

        # Signal-gate: a read-only last action on the same page needs no re-snapshot.
        reuse = (
            isinstance(prev, dict) and prev_url and not last_error
            and last_tool in _READ_ONLY_TOOLS
        )
        if reuse:
            data = dict(prev)
            data["reused"] = True
            signals = "no page change since last step (read-only action)"
            self._write(data, signals)
            return

        snapshot_text = self._call_snapshot(server_entry)
        summary = summarize_actionable_snapshot(snapshot_text, max_elements=_MAX_ELEMENTS)
        refs = [str(el.get("ref") or "") for el in summary.get("actionable_elements", []) if el.get("ref")]

        url = str(summary.get("url") or "")
        new_refs = [r for r in refs if r and r not in prev_refs] if prev_refs else []
        signal_parts: list[str] = []
        if last_error:
            signal_parts.append("LAST ACTION FAILED — re-read the page before retrying")
        if prev_url and url and url != prev_url:
            signal_parts.append(f"URL CHANGED ({prev_url} -> {url})")
        if new_refs:
            signal_parts.append(f"{len(new_refs)} new element(s) appeared")
        if not signal_parts:
            signal_parts.append("page looks the same as before this action")

        summary["_refs"] = refs
        summary["reused"] = False
        self._write(summary, "; ".join(signal_parts))

    def _write(self, data: dict[str, Any], signals: str) -> None:
        self.blackboard.update_state_value("playwright2_observation_data", data)
        self.blackboard.update_state_value("playwright2_signals", signals)
        self.blackboard.update_state_value("playwright2_observation", self._format(data, signals))

    def _call_snapshot(self, server_entry: dict[str, Any]) -> str:
        resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_SNAPSHOT,
            arguments={},
            timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
        )
        text, is_error, _ = format_mcp_tool_result_content(resp)
        if is_error:
            raise RuntimeError(f"browser_snapshot failed: {text}")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _format(data: dict[str, Any], signals: str) -> str:
        url = str(data.get("url") or "").strip()
        title = str(data.get("title") or "").strip()
        if not url or url == "about:blank":
            return ("PAGE: about:blank — the browser is on a blank tab. "
                    "Navigate to the target site first (web_navigate_snapshot).")

        els = data.get("actionable_elements") or []
        total = int(data.get("actionable_total") or len(els))
        header = f"PAGE: {url}" + (f" ({title})" if title else "")
        lines = [header, f"SIGNALS: {signals}"]
        shown = len(els)
        lines.append(f"ACTIONABLE ELEMENTS (showing {shown} of {total}"
                     + (", scroll for more" if data.get("truncated") else "") + "):")
        for el in els:
            role = str(el.get("role") or "").strip() or "element"
            label = str(el.get("text") or "").strip()   # summarize stores the label under "text"
            ref = str(el.get("ref") or "").strip()
            label_part = f' "{label}"' if label else ""
            lines.append(f"  [{ref}] {role}{label_part}")
        lines.append("Click/type by [ref]. For page TEXT (articles, prices, body copy) "
                     "the snapshot won't have it — use web_get_content.")
        return "\n".join(lines)
