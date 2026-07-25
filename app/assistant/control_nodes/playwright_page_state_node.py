from __future__ import annotations

from typing import Any

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.mcp.tool_runner import format_mcp_tool_result_content, mcp_stdio_call_tool
from app.assistant.utils.json_parsing import parse_jsonish
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_last_tool_result_meta

logger = get_logger(__name__)


class PlaywrightPageStateNode(ControlNode):
    """
    Detect modal presence, auto-focus modal area, and inject planner status.

    This node runs after tool return routing. It:
    - probes DOM for a visible modal/dialog and basic scrollable body signal
    - moves mouse to a safe focus point inside the modal when present
    - writes `playwright_status` to blackboard for planner prompt injection
    - routes to configured next node
    """

    SERVER_ID = "npm/playwright-mcp"
    MCP_EVALUATE = "browser_evaluate"
    MCP_MOVE_MOUSE = "browser_mouse_move_xy"

    def action_handler(self, message):  # noqa: ARG002
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        source_agent = self._required_str(cfg, "source_agent")
        next_agent = self._required_str(cfg, "next_agent")
        status_key = self._optional_str(cfg, "status_key", "playwright_status")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != source_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={source_agent} before page-state route, got: {last_agent!r}"
            )

        server_entry = self.tool_registry.get_mcp_server_entry(self.SERVER_ID)
        if not isinstance(server_entry, dict):
            raise RuntimeError(f"[{self.name}] Missing MCP server entry for {self.SERVER_ID}.")

        self._maybe_settle_after_action(server_entry=server_entry, cfg=cfg)
        self._maybe_wait_for_readiness(server_entry=server_entry, cfg=cfg)

        # Modal detection disabled — false positives (DoorDash promo banners,
        # dropdowns with role="dialog") caused the planner to waste all its time
        # dismissing phantom modals. Real modals are detected reactively: if a
        # click fails because something is blocking, the planner handles it.
        self.blackboard.update_state_value(status_key, "")

        # Page identity — the planner must always know WHERE the browser is.
        # A blank tab is the silent failure mode: every look/click tool returns
        # "nothing found" until someone navigates (2026-07-25 DoorDash incident).
        try:
            ident = self._probe_ready_state(server_entry=server_entry)
        except Exception:
            logger.debug("[%s] page identity probe failed", self.name, exc_info=True)
            ident = {}
        self.blackboard.update_state_value("playwright_page_identity_data", ident)
        self.blackboard.update_state_value("playwright_page_identity", self._format_page_identity(ident))

        # Page scroll state (lightweight, no modal detection needed).
        try:
            page_scroll = self._get_page_scroll_state(server_entry)
            scroll_data = {"page": page_scroll} if page_scroll else {}
            self.blackboard.update_state_value("playwright_scroll_state_data", scroll_data)
            self.blackboard.update_state_value("playwright_scroll_state", self._format_scroll_state(scroll_data))
        except Exception:
            logger.debug("[%s] Failed to get page scroll state", self.name, exc_info=True)
            self.blackboard.update_state_value("playwright_scroll_state_data", {})
            self.blackboard.update_state_value("playwright_scroll_state", "")
        self.blackboard.update_state_value("playwright_modal_form_state", "")
        self.blackboard.update_state_value("playwright_modal_state", None)
        self.blackboard.update_state_value("playwright_modal_bbox", None)
        self.blackboard.update_state_value("playwright_modal_focus_point", None)

        self.blackboard.update_state_value("next_agent", next_agent)
        # Keep strict summary source contract intact when this node is inserted before summary_pre_node.
        self.blackboard.update_state_value("last_agent", source_agent)

    def _maybe_settle_after_action(self, *, server_entry: dict[str, Any], cfg: dict[str, Any]) -> None:
        settle_cfg = cfg.get("post_action_settle")
        if settle_cfg is None:
            settle_cfg = {}
        if not isinstance(settle_cfg, dict):
            raise ValueError("playwright_page_state.post_action_settle must be a dict when provided.")
        if not bool(settle_cfg.get("enabled", True)):
            return

        run_after_tools_raw = settle_cfg.get(
            "run_after_tools",
            [
                "web_click_ref_snapshot",
                "web_click_xy_snapshot",
                "web_fill_xy",
                "web_type_focused",
                "mcp::npm/playwright-mcp::browser_click",
                "mcp::npm/playwright-mcp::browser_mouse_click_xy",
                "mcp::npm/playwright-mcp::browser_type",
            ],
        )
        if not isinstance(run_after_tools_raw, list):
            raise ValueError("playwright_page_state.post_action_settle.run_after_tools must be a list.")
        run_after_tools = {str(t).strip() for t in run_after_tools_raw if isinstance(t, str) and str(t).strip()}

        last_meta = get_last_tool_result_meta(self.blackboard) or {}
        last_tool_name = ""
        if isinstance(last_meta, dict):
            tn = last_meta.get("tool_name")
            if isinstance(tn, str) and tn.strip():
                last_tool_name = tn.strip()
        if run_after_tools and last_tool_name not in run_after_tools:
            return

        settle_ms = int(settle_cfg.get("settle_ms", 1000) or 1000)
        if settle_ms < 0:
            raise ValueError("playwright_page_state.post_action_settle.settle_ms must be >= 0")
        if settle_ms == 0:
            return

        logger.debug(
            "[%s] post_action_settle waiting %sms after tool=%r before modal/status detection",
            self.name,
            settle_ms,
            last_tool_name,
        )
        _ = self._call_mcp_text(
            server_entry=server_entry,
            tool_name="browser_wait_for",
            arguments={"time": float(settle_ms) / 1000.0},
        )

    def _maybe_wait_for_readiness(self, *, server_entry: dict[str, Any], cfg: dict[str, Any]) -> None:
        gate_cfg = cfg.get("readiness_gate")
        if gate_cfg is None:
            return
        if not isinstance(gate_cfg, dict):
            raise ValueError("playwright_page_state.readiness_gate must be a dict when provided.")
        if not bool(gate_cfg.get("enabled", False)):
            return

        run_after_tools_raw = gate_cfg.get(
            "run_after_tools",
            [
                "mcp::npm/playwright-mcp::browser_navigate",
                "mcp::npm/playwright-mcp::browser_navigate_back",
                "mcp::npm/playwright-mcp::browser_tabs",
            ],
        )
        if not isinstance(run_after_tools_raw, list):
            raise ValueError("playwright_page_state.readiness_gate.run_after_tools must be a list.")
        run_after_tools = {str(t).strip() for t in run_after_tools_raw if isinstance(t, str) and str(t).strip()}

        last_meta = self.blackboard.get_state_value("last_tool_result_meta", None)
        last_tool_name = ""
        if isinstance(last_meta, dict):
            tn = last_meta.get("tool_name")
            if isinstance(tn, str) and tn.strip():
                last_tool_name = tn.strip()
        if run_after_tools and last_tool_name not in run_after_tools:
            return

        max_attempts = int(gate_cfg.get("max_attempts", 8) or 8)
        sleep_ms = int(gate_cfg.get("sleep_ms", 300) or 300)
        require_complete = bool(gate_cfg.get("require_complete", True))
        if max_attempts <= 0:
            raise ValueError("playwright_page_state.readiness_gate.max_attempts must be > 0")
        if sleep_ms < 0:
            raise ValueError("playwright_page_state.readiness_gate.sleep_ms must be >= 0")

        last_probe: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            last_probe = self._probe_ready_state(server_entry=server_entry)
            ready_state = str(last_probe.get("readyState") or "").strip().lower()
            has_body = bool(last_probe.get("hasBody", False))
            is_ready = has_body and (
                ready_state == "complete" if require_complete else ready_state in {"interactive", "complete"}
            )
            if is_ready:
                self.blackboard.update_state_value("playwright_readiness_state", last_probe)
                return
            if attempt < max_attempts and sleep_ms > 0:
                _ = self._call_mcp_text(
                    server_entry=server_entry,
                    tool_name="browser_wait_for",
                    arguments={"time": float(sleep_ms) / 1000.0},
                )

        raise RuntimeError(
            f"[{self.name}] Readiness gate failed after {max_attempts} attempts. "
            f"last_tool={last_tool_name!r} probe={last_probe!r}"
        )

    def _probe_ready_state(self, *, server_entry: dict[str, Any]) -> dict[str, Any]:
        js = """
() => {
    const rs = String(document.readyState || "");
    const hasBody = !!document.body;
    const href = String(location.href || "");
    const title = String(document.title || "");
    return {
      readyState: rs,
      hasBody: hasBody,
      href: href,
      title: title
    };
}
""".strip()
        text = self._call_mcp_text(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": js},
        )
        parsed = parse_jsonish(text)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"[{self.name}] Readiness probe parse failed. payload={str(text)[:500]!r}")
        return parsed

    def _detect_modal_state(self, server_entry: dict[str, Any]) -> dict[str, Any]:
        js = r"""
() => {
    const norm = (v, maxLen = 120) => {
      const s = String(v || "").replace(/\s+/g, " ").trim();
      return s.length > maxLen ? s.slice(0, maxLen) : s;
    };

    const toScrollState = (el) => {
      if (!el) {
        return {
          scrollable: false,
          scrollTop: 0,
          scrollHeight: 0,
          clientHeight: 0,
          maxScrollTop: 0,
          atTop: true,
          atBottom: true,
          progress: 1.0,
        };
      }
      const scrollTop = Number(el.scrollTop || 0);
      const scrollHeight = Number(el.scrollHeight || 0);
      const clientHeight = Number(el.clientHeight || 0);
      const maxScrollTop = Math.max(0, scrollHeight - clientHeight);
      const scrollable = maxScrollTop > 1;
      const atTop = scrollTop <= 1;
      const atBottom = scrollTop >= (maxScrollTop - 1);
      const progress = scrollable ? Math.min(1, Math.max(0, scrollTop / Math.max(1, maxScrollTop))) : 1.0;
      return {
        scrollable,
        scrollTop: Math.round(scrollTop),
        scrollHeight: Math.round(scrollHeight),
        clientHeight: Math.round(clientHeight),
        maxScrollTop: Math.round(maxScrollTop),
        atTop,
        atBottom,
        progress: Number(progress.toFixed(3)),
      };
    };

    const root = document.scrollingElement || document.documentElement || document.body;
    const pageScroll = toScrollState(root);

    const isVisible = (el) => {
      if (!el) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 12 || r.height < 12) return false;
      const cs = window.getComputedStyle(el);
      if (!cs) return false;
      if (cs.display === "none" || cs.visibility === "hidden") return false;
      if (Number(cs.opacity || "1") < 0.05) return false;
      return true;
    };

    // --- Modal detection ---
    const vw = window.innerWidth || 1;
    const vh = window.innerHeight || 1;
    const vpArea = vw * vh;

    const isBlockingPosition = (el) => {
      const cs = window.getComputedStyle(el);
      const pos = (cs.position || "").toLowerCase();
      return pos === "fixed" || pos === "absolute" || pos === "sticky";
    };

    // Tier 1 (high confidence): semantic ARIA/role/tag markers.
    // Must ALSO be visible, positioned (fixed/absolute), and cover >= 15% of viewport
    // to avoid false positives from dropdowns, autocompletes, and promo sections
    // that use role="dialog" without actually blocking interaction.
    const tier1 = [];
    const tier1Selectors = [
      "dialog[open]",
      "[role='dialog']",
      "[role='alertdialog']",
      "[aria-modal='true']",
    ];
    for (const sel of tier1Selectors) {
      document.querySelectorAll(sel).forEach((el) => {
        if (!isVisible(el)) return;
        if (!isBlockingPosition(el)) return;
        const r = el.getBoundingClientRect();
        if ((r.width * r.height) / vpArea < 0.15) return;
        tier1.push(el);
      });
    }

    // Tier 2 (medium confidence): class/data-attr hints.
    // Only count these if the element is ALSO positioned to block interaction
    // (fixed/absolute with a backdrop or covering significant viewport area).
    const tier2Hints = [];
    const tier2Selectors = [
      "[data-modal]",
      "[data-testid*='modal' i]",
      "[data-testid*='dialog' i]",
      "[data-testid*='drawer' i]",
      "[data-testid*='sheet' i]",
    ];
    // Class-based: require the class to be a primary component class, not a
    // modifier substring. Match classes that START with or ARE the keyword.
    const classPatterns = [
      /\bmodal\b/i,
      /\bModal\b/,
      /\bdialog\b/i,
      /\bbottom-?sheet\b/i,
      /\bdrawer\b/i,
    ];
    for (const sel of tier2Selectors) {
      document.querySelectorAll(sel).forEach((el) => tier2Hints.push(el));
    }
    document.querySelectorAll("[class]").forEach((el) => {
      const cls = el.getAttribute("class") || "";
      if (classPatterns.some((p) => p.test(cls))) {
        tier2Hints.push(el);
      }
    });

    // Validate tier 2: must be fixed/absolute AND cover >= 20% viewport,
    // OR have a sibling/parent backdrop element.

    const hasBackdrop = (el) => {
      // Check for a sibling or parent backdrop overlay.
      const parent = el.parentElement;
      if (!parent) return false;
      for (const sibling of parent.children) {
        if (sibling === el) continue;
        const cls = (sibling.getAttribute("class") || "").toLowerCase();
        const role = (sibling.getAttribute("role") || "").toLowerCase();
        if (cls.includes("backdrop") || cls.includes("overlay") || role === "presentation") {
          const sr = sibling.getBoundingClientRect();
          if (sr.width * sr.height > vpArea * 0.3) return true;
        }
      }
      return false;
    };

    const validatedTier2 = tier2Hints.filter((el) => {
      if (!isVisible(el)) return false;
      const r = el.getBoundingClientRect();
      const elArea = r.width * r.height;
      // Must cover meaningful viewport area AND be positioned to block
      if (isBlockingPosition(el) && elArea / vpArea >= 0.2) return true;
      // OR have a backdrop sibling
      if (hasBackdrop(el)) return true;
      return false;
    });

    // Tier 3 (low confidence): large fixed elements with buttons.
    // Higher threshold than before (50% instead of 35%), and must have
    // at least 2 interactive elements to distinguish from sticky headers.
    const tier3 = [];
    const allFixed = Array.from(document.querySelectorAll("*")).filter((el) => {
      const cs = window.getComputedStyle(el);
      return (cs.position || "") === "fixed";
    });
    for (const el of allFixed) {
      const r = el.getBoundingClientRect();
      if (r.width < 200 || r.height < 150) continue;
      const elArea = r.width * r.height;
      if (elArea / vpArea < 0.50) continue;
      const buttons = el.querySelectorAll("button, [role='button'], input[type='submit']");
      if (buttons.length < 2) continue;
      tier3.push(el);
    }

    const candidates = [...tier1, ...validatedTier2, ...tier3];
    const unique = Array.from(new Set(candidates)).filter(isVisible);
    if (!unique.length) {
      return {
        modal_open: false,
        modal_bbox: null,
        focus_point: null,
        scrollable_found: false,
        modal_scroll: null,
        page_scroll: pageScroll,
        form_state: {
          has_form: false,
          controls: [],
          radio_groups: {},
          selected_items: [],
          unselected_items: [],
          buttons: [],
        },
      };
    }

    const scoreEl = (el) => {
      const r = el.getBoundingClientRect();
      const area = Math.max(0, r.width * r.height);
      let score = area;
      if ((el.getAttribute("aria-modal") || "").toLowerCase() === "true") score += 5_000_000;
      const role = (el.getAttribute("role") || "").toLowerCase();
      if (role === "dialog" || role === "alertdialog") score += 3_000_000;
      if (el.tagName && el.tagName.toLowerCase() === "dialog") score += 2_000_000;
      return score;
    };
    unique.sort((a, b) => scoreEl(b) - scoreEl(a));
    const modal = unique[0];
    const m = modal.getBoundingClientRect();

    const isScrollable = (el) => {
      if (!el) return false;
      const cs = window.getComputedStyle(el);
      const oy = (cs.overflowY || "").toLowerCase();
      const allows = oy === "auto" || oy === "scroll" || oy === "overlay";
      return allows && el.scrollHeight > (el.clientHeight + 8);
    };

    let focusEl = null;
    const walker = document.createTreeWalker(modal, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (isScrollable(node)) {
        focusEl = node;
        break;
      }
    }
    if (!focusEl) focusEl = modal;
    const modalScroll = toScrollState(focusEl);

    const f = focusEl.getBoundingClientRect();
    const fx = Math.round(f.left + Math.max(12, f.width / 2));
    const fy = Math.round(f.top + Math.max(12, f.height / 2));

    const labelFor = (el) => {
      if (!el) return "";
      const aria = norm(el.getAttribute && el.getAttribute("aria-label"));
      if (aria) return aria;
      const id = norm(el.id);
      if (id) {
        const viaFor = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (viaFor) {
          const t = norm(viaFor.innerText || viaFor.textContent);
          if (t) return t;
        }
      }
      const wrap = el.closest && el.closest("label");
      if (wrap) {
        const t = norm(wrap.innerText || wrap.textContent);
        if (t) return t;
      }
      const txt = norm(el.innerText || el.textContent);
      if (txt) return txt;
      const ph = norm(el.getAttribute && el.getAttribute("placeholder"));
      if (ph) return ph;
      return "";
    };

    const controls = [];
    const radioGroups = {};
    const selectedItems = [];
    const unselectedItems = [];
    const buttons = [];
    const nodes = Array.from(modal.querySelectorAll("input, select, textarea, button")).slice(0, 90);
    for (const el of nodes) {
      const tag = String((el.tagName || "").toLowerCase());
      let type = tag;
      if (tag === "input") type = String((el.getAttribute("type") || "text").toLowerCase());
      const name = norm(el.getAttribute && el.getAttribute("name"));
      const id = norm(el.id);
      const label = labelFor(el);
      const value = norm(el.value);
      const visible = isVisible(el);
      const disabled = !!el.disabled;
      const checked = (type === "radio" || type === "checkbox") ? !!el.checked : null;

      const row = {
        type,
        tag,
        name: name || null,
        id: id || null,
        label: label || null,
        value: value || null,
        visible,
        disabled,
        checked,
      };
      controls.push(row);

      if (type === "radio") {
        const g = name || "__unnamed__";
        if (!radioGroups[g]) radioGroups[g] = [];
        radioGroups[g].push({
          label: label || value || id || "(unlabeled)",
          value: value || null,
          checked: !!el.checked,
          visible,
          disabled,
        });
        if (el.checked) {
          selectedItems.push(`radio ${g}: ${label || value || id || "(unlabeled)"}`);
        } else {
          unselectedItems.push(`radio ${g}: ${label || value || id || "(unlabeled)"}`);
        }
      } else if (type === "checkbox") {
        if (el.checked) {
          selectedItems.push(`checkbox: ${label || value || id || "(unlabeled)"}`);
        } else {
          unselectedItems.push(`checkbox: ${label || value || id || "(unlabeled)"}`);
        }
      } else if (tag === "select") {
        const selectedOpt = Array.from(el.selectedOptions || []).map((o) => norm(o && (o.textContent || o.value)));
        if (selectedOpt.length) {
          selectedItems.push(`select ${name || id || "(unnamed)"}: ${selectedOpt.join(", ")}`);
        }
      }

      if (tag === "button" || type === "button" || type === "submit") {
        buttons.push({
          label: label || value || id || "(unlabeled)",
          disabled,
          visible,
        });
      }
    }

    return {
      modal_open: true,
      modal_bbox: { left: Math.round(m.left), top: Math.round(m.top), width: Math.round(m.width), height: Math.round(m.height) },
      focus_point: { x: fx, y: fy },
      scrollable_found: focusEl !== modal,
      modal_scroll: modalScroll,
      page_scroll: pageScroll,
      form_state: {
        has_form: controls.length > 0,
        controls,
        radio_groups: radioGroups,
        selected_items: selectedItems.slice(0, 30),
        unselected_items: unselectedItems.slice(0, 30),
        buttons: buttons.slice(0, 20),
      },
    };
}
""".strip()
        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": js},
            timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
        )
        text, is_error, _attachments = format_mcp_tool_result_content(call_resp)
        if is_error:
            raise RuntimeError(f"[{self.name}] MCP tool {self.MCP_EVALUATE} failed: {text}")

        result = call_resp.get("result")
        if isinstance(result, dict):
            structured = result.get("structuredContent")
            if isinstance(structured, dict):
                return structured

        payload = parse_jsonish(text)
        if not isinstance(payload, dict):
            preview = (text or "").strip().replace("\n", "\\n")[:600]
            raise RuntimeError(f"[{self.name}] Could not parse modal detector payload. preview={preview!r}")
        modal_open = bool(payload.get("modal_open", False))
        logger.debug(
            "[%s] modal detector result: modal_open=%s bbox=%s",
            self.name,
            modal_open,
            payload.get("modal_bbox"),
        )
        return payload

    def _move_mouse(self, *, server_entry: dict[str, Any], x: float, y: float) -> None:
        _ = self._call_mcp_text(
            server_entry=server_entry,
            tool_name=self.MCP_MOVE_MOUSE,
            arguments={"x": int(round(x)), "y": int(round(y))},
        )

    def _call_mcp_text(self, *, server_entry: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> str:
        call_resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=tool_name,
            arguments=arguments,
            timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)),
        )
        text, is_error, _attachments = format_mcp_tool_result_content(call_resp)
        if is_error:
            raise RuntimeError(f"[{self.name}] MCP tool {tool_name} failed: {text}")
        return text if isinstance(text, str) else ""


    def _run_js(self, *, server_entry: dict[str, Any], js: str) -> str:
        """Call browser_evaluate with the given JS snippet. Returns raw response text.

        Raises on MCP-level failure so the caller's broad except logs once.
        """
        timeout_s = float(server_entry.get("policy", {}).get("call_timeout_seconds", 20))
        resp = mcp_stdio_call_tool(
            server_entry=server_entry,
            tool_name=self.MCP_EVALUATE,
            arguments={"function": js},
            timeout_s=timeout_s,
        )
        text, is_error, _ = format_mcp_tool_result_content(resp)
        if is_error:
            raise RuntimeError(f"browser_evaluate failed: {text}")
        return text or ""

    def _get_page_scroll_state(self, server_entry: dict[str, Any]) -> dict[str, Any]:
        """Lightweight page scroll state — no modal detection overhead."""
        js = """
() => {
    const root = document.scrollingElement || document.documentElement || document.body;
    const scrollTop = Number(root.scrollTop || 0);
    const scrollHeight = Number(root.scrollHeight || 0);
    const clientHeight = Number(root.clientHeight || 0);
    const maxScrollTop = Math.max(0, scrollHeight - clientHeight);
    const scrollable = maxScrollTop > 1;
    return {
      scrollable,
      scrollTop: Math.round(scrollTop),
      maxScrollTop: Math.round(maxScrollTop),
      atTop: scrollTop <= 1,
      atBottom: scrollTop >= (maxScrollTop - 1),
      progress: scrollable ? Number((scrollTop / Math.max(1, maxScrollTop)).toFixed(3)) : 1.0,
    };
}"""
        try:
            raw = self._run_js(server_entry=server_entry, js=js)
            payload = self._extract_json_payload(raw)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            logger.debug("[%s] _get_page_scroll_state failed", self.name, exc_info=True)
            return {}

    @staticmethod
    def _format_page_identity(ident: dict[str, Any]) -> str:
        if not isinstance(ident, dict) or not ident:
            return "Current page: unknown (identity probe failed)."
        href = str(ident.get("href") or "").strip()
        title = str(ident.get("title") or "").strip()
        if not href or href == "about:blank":
            return (
                "Current page: about:blank — the browser is on a BLANK TAB with "
                "nothing loaded. Navigate to the target site first (web_navigate_snapshot)."
            )
        line = f"Current page: {href}"
        if title:
            line += f" ({title})"
        return line

    @staticmethod
    def _extract_scroll_state(modal_state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(modal_state, dict):
            return {}
        page_scroll = modal_state.get("page_scroll")
        modal_scroll = modal_state.get("modal_scroll")
        out: dict[str, Any] = {}
        if isinstance(page_scroll, dict):
            out["page"] = page_scroll
        if isinstance(modal_scroll, dict):
            out["modal"] = modal_scroll
        return out

    @staticmethod
    def _extract_modal_form_state(modal_state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(modal_state, dict):
            return {}
        form_state = modal_state.get("form_state")
        if not isinstance(form_state, dict):
            return {}
        return form_state

    @staticmethod
    def _format_scroll_state(scroll_state: dict[str, Any]) -> str:
        if not isinstance(scroll_state, dict) or not scroll_state:
            return "Scroll state unavailable."

        def _line(label: str, data: Any) -> str | None:
            if not isinstance(data, dict):
                return None
            scrollable = bool(data.get("scrollable", False))
            progress = data.get("progress", 1.0)
            at_top = bool(data.get("atTop", True))
            at_bottom = bool(data.get("atBottom", True))
            top = int(data.get("scrollTop", 0) or 0)
            max_top = int(data.get("maxScrollTop", 0) or 0)
            return (
                f"- {label}: scrollable={str(scrollable).lower()} "
                f"progress={progress} at_top={str(at_top).lower()} at_bottom={str(at_bottom).lower()} "
                f"scroll_top={top}/{max_top}"
            )

        lines: list[str] = []
        modal_line = _line("modal", scroll_state.get("modal"))
        page_line = _line("page", scroll_state.get("page"))
        if modal_line:
            lines.append(modal_line)
        if page_line:
            lines.append(page_line)
        if not lines:
            return "Scroll state unavailable."
        return "Scroll state:\n" + "\n".join(lines)

    @staticmethod
    def _format_modal_form_state(form_state: dict[str, Any]) -> str:
        if not isinstance(form_state, dict) or not form_state:
            return "Modal form state unavailable."
        if not bool(form_state.get("has_form", False)):
            return "Modal form state: no form controls detected."

        selected = form_state.get("selected_items")
        unselected = form_state.get("unselected_items")
        buttons = form_state.get("buttons")
        controls = form_state.get("controls")
        selectable_refs = form_state.get("selectable_refs")
        lines: list[str] = []
        lines.append(
            "Modal form state:"
            f" controls={len(controls) if isinstance(controls, list) else 0}"
        )
        if isinstance(selectable_refs, list):
            lines.append(f"- selectable_refs: {len(selectable_refs)}")
            if selectable_refs:
                lines.append("- recommended_ref_clicks:")
                for row in selectable_refs[:10]:
                    if not isinstance(row, dict):
                        continue
                    ref = str(row.get("ref") or "").strip()
                    role = str(row.get("role") or "").strip()
                    text = str(row.get("text") or "").strip()
                    if ref:
                        lines.append(f"  - ref={ref} role={role or 'unknown'} text={text or '(no label)'}")

        if isinstance(selected, list) and selected:
            lines.append("- selected:")
            for item in selected[:8]:
                if isinstance(item, dict):
                    label = str(item.get("label") or item.get("text") or "").strip()
                    ref = str(item.get("ref") or "").strip()
                    if ref:
                        lines.append(f"  - {label} [ref={ref}]")
                    elif label:
                        lines.append(f"  - {label}")
                elif isinstance(item, str) and item.strip():
                    lines.append(f"  - {item.strip()}")
        if isinstance(unselected, list) and unselected:
            lines.append("- not_selected:")
            for item in unselected[:8]:
                if isinstance(item, dict):
                    label = str(item.get("label") or item.get("text") or "").strip()
                    ref = str(item.get("ref") or "").strip()
                    if ref:
                        lines.append(f"  - {label} [ref={ref}]")
                    elif label:
                        lines.append(f"  - {label}")
                elif isinstance(item, str) and item.strip():
                    lines.append(f"  - {item.strip()}")
        if isinstance(buttons, list) and buttons:
            lines.append("- buttons:")
            for b in buttons[:6]:
                if not isinstance(b, dict):
                    continue
                label = str(b.get("label") or "(unlabeled)").strip()
                disabled = bool(b.get("disabled", False))
                visible = bool(b.get("visible", False))
                lines.append(
                    f"  - {label} (disabled={str(disabled).lower()}, visible={str(visible).lower()})"
                )
        return "\n".join(lines)

    @staticmethod
    def _attach_form_refs(form_state: dict[str, Any], latest_snapshot: Any) -> dict[str, Any]:
        if not isinstance(form_state, dict):
            return {}
        if not isinstance(latest_snapshot, dict):
            return form_state
        elements = latest_snapshot.get("actionable_elements")
        if not isinstance(elements, list):
            return form_state

        def _norm(s: Any) -> str:
            return str(s or "").strip().lower()

        lookup: list[dict[str, str]] = []
        for e in elements:
            if not isinstance(e, dict):
                continue
            ref = str(e.get("ref") or "").strip()
            if not ref:
                continue
            role = str(e.get("role") or "").strip().lower()
            text = str(e.get("text") or "").strip()
            lookup.append({"ref": ref, "role": role, "text": text, "norm": _norm(text)})

        def _pick_ref(label: str) -> dict[str, str] | None:
            nl = _norm(label)
            if not nl:
                return None
            # Exact preferred, then contains, then reverse contains.
            for row in lookup:
                if row["norm"] == nl:
                    return {"ref": row["ref"], "role": row["role"], "text": row["text"]}
            for row in lookup:
                if nl and nl in row["norm"]:
                    return {"ref": row["ref"], "role": row["role"], "text": row["text"]}
            for row in lookup:
                if row["norm"] and row["norm"] in nl:
                    return {"ref": row["ref"], "role": row["role"], "text": row["text"]}
            return None

        selectable_refs: list[dict[str, str]] = []
        selected_out: list[dict[str, Any] | str] = []
        unselected_out: list[dict[str, Any] | str] = []

        for key, out_list in (("selected_items", selected_out), ("unselected_items", unselected_out)):
            vals = form_state.get(key)
            if not isinstance(vals, list):
                continue
            for item in vals:
                label = str(item or "").strip()
                if not label:
                    continue
                ref_row = _pick_ref(label)
                if ref_row:
                    row = {"label": label, "ref": ref_row["ref"], "role": ref_row["role"], "text": ref_row["text"]}
                    out_list.append(row)
                    selectable_refs.append({"ref": ref_row["ref"], "role": ref_row["role"], "text": ref_row["text"]})
                else:
                    out_list.append(label)

        out = dict(form_state)
        if selected_out:
            out["selected_items"] = selected_out
        if unselected_out:
            out["unselected_items"] = unselected_out
        # de-dup refs while preserving order
        seen: set[str] = set()
        uniq: list[dict[str, str]] = []
        for row in selectable_refs:
            ref = str(row.get("ref") or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            uniq.append(row)
        out["selectable_refs"] = uniq[:20]
        return out

    @staticmethod
    def _compact_bbox_str(bbox: Any) -> str:
        if not isinstance(bbox, dict):
            return "unknown"
        left = bbox.get("left")
        top = bbox.get("top")
        width = bbox.get("width")
        height = bbox.get("height")
        if all(isinstance(v, (int, float)) for v in [left, top, width, height]):
            return f"({int(left)},{int(top)},{int(width)},{int(height)})"
        return "unknown"

    @staticmethod
    def _format_modal_snapshot_card(
        *,
        modal_state: dict[str, Any],
        form_state: dict[str, Any],
        latest_snapshot: dict[str, Any],
    ) -> str:
        """
        Build a focused snapshot card that only shows modal-relevant elements.

        When a modal is open, the full-page snapshot is noisy — most elements are
        behind the modal and not interactable. This replaces it with a clean view
        of the modal's state.
        """
        bbox = modal_state.get("modal_bbox")
        focus = modal_state.get("focus_point")
        scroll = modal_state.get("modal_scroll") or {}
        sid = str(latest_snapshot.get("snapshot_id") or "").strip()
        url = str(latest_snapshot.get("url") or "").strip()
        title = str(latest_snapshot.get("title") or "").strip()

        lines: list[str] = ["Latest snapshot (MODAL FOCUSED):"]
        if sid:
            lines.append(f"- snapshot_id: {sid}")
        if url:
            lines.append(f"- url: {url}")
        if title:
            lines.append(f"- title: {title}")
        lines.append("- modal_open: true")
        lines.append("- background_suppressed: true")
        if isinstance(bbox, dict):
            lines.append(
                f"- modal_bbox: left={bbox.get('left',0)} top={bbox.get('top',0)} "
                f"width={bbox.get('width',0)} height={bbox.get('height',0)}"
            )
        if isinstance(focus, dict):
            lines.append(f"- modal_focus: ({focus.get('x',0)}, {focus.get('y',0)})")
        if isinstance(scroll, dict):
            scrollable = bool(scroll.get("scrollable", False))
            lines.append(
                f"- modal_scrollable: {str(scrollable).lower()}"
            )
            if scrollable:
                lines.append(f"- modal_scroll_progress: {scroll.get('progress', 1.0)}")
                lines.append(
                    f"- modal_at_top: {str(scroll.get('atTop', True)).lower()} "
                    f"modal_at_bottom: {str(scroll.get('atBottom', True)).lower()}"
                )

        # Modal actionable elements: form controls + buttons from form_state,
        # enriched with snapshot refs via _attach_form_refs.
        if isinstance(form_state, dict):
            controls = form_state.get("controls")
            control_count = len(controls) if isinstance(controls, list) else 0
            lines.append(f"- modal_controls: {control_count}")

            selectable_refs = form_state.get("selectable_refs")
            if isinstance(selectable_refs, list) and selectable_refs:
                lines.append("- modal_actionable_elements:")
                for row in selectable_refs[:20]:
                    if not isinstance(row, dict):
                        continue
                    ref = str(row.get("ref") or "").strip()
                    role = str(row.get("role") or "").strip()
                    text = str(row.get("text") or "").strip()
                    if ref:
                        lines.append(f"  - {ref} [{role or 'unknown'}] {text or '(no label)'}")

            buttons = form_state.get("buttons")
            if isinstance(buttons, list) and buttons:
                lines.append("- modal_buttons:")
                for b in buttons[:10]:
                    if not isinstance(b, dict):
                        continue
                    label = str(b.get("label") or "(unlabeled)").strip()
                    disabled = bool(b.get("disabled", False))
                    visible = bool(b.get("visible", False))
                    ref = str(b.get("ref") or "").strip()
                    ref_s = f" ref={ref}" if ref else ""
                    lines.append(
                        f"  - {label} (disabled={str(disabled).lower()}, visible={str(visible).lower()}){ref_s}"
                    )

        return "\n".join(lines)

    def _cfg(self) -> dict[str, Any]:
        return self._flow_section_cfg("playwright_page_state")
