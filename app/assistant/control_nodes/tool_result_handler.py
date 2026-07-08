import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.tool_error_protocol import validate_tool_error_contract
from app.assistant.utils.pydantic_classes import Message, ToolResult
from app.assistant.manager_runtime.services.tool_scope_state_manager import ToolScopeStateManager
from app.assistant.utils.pipeline_state import (
    get_pending_tool,
    clear_pending_tool,
    set_last_tool_result_ref,
    get_flag,
)
from app.assistant.control_nodes.control_node import ControlNode

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
logger = get_logger(__name__)


def _tool_results_dir() -> Path:
    from app.assistant.utils.path_utils import get_uploads_dir
    return get_uploads_dir() / "temp" / "tool_results"


def _prune_tool_results_dir(*, tool_results_dir: Path, max_files: int = 500, max_age_hours: int = 72) -> None:
    """
    Best-effort retention policy for persisted tool results.

    Why:
    - Many tool calls happen in tight loops (e.g., browser automation).
    - Persisting every ToolResult payload without retention can create unbounded disk usage.
    """
    try:
        if not tool_results_dir.exists():
            return

        max_files = int(max_files) if int(max_files) > 0 else 500
        max_age_hours = int(max_age_hours) if int(max_age_hours) > 0 else 72

        now_ts = datetime.now(timezone.utc).timestamp()
        max_age_s = float(max_age_hours) * 3600.0

        # Only prune our own files.
        files = list(tool_results_dir.glob("tool_result_*.json"))
        if not files:
            return

        # 1) Age-based pruning (cheap).
        for p in files:
            try:
                st = p.stat()
                if (now_ts - float(st.st_mtime)) > max_age_s:
                    p.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to prune tool result file %s", p, exc_info=True)
                continue

        # 2) Count-based pruning (keep most recent N).
        files = list(tool_results_dir.glob("tool_result_*.json"))
        if len(files) <= max_files:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to prune tool result file %s", p, exc_info=True)
                continue
    except Exception:
        # Best-effort only; never break tool handling.
        logger.debug("Tool result dir pruning failed", exc_info=True)
        return


def _persist_tool_result_artifact(
    *,
    tool_result,
    calling_agent: str | None,
    scope_id: str | None,
    max_files: int = 500,
    max_age_hours: int = 72,
) -> dict[str, str] | None:
    """
    Persist a full ToolResult payload to disk and return a small reference dict.
    Intended to keep future prompts small while preserving the full payload for on-demand retrieval.
    """
    try:
        tool_results_dir = _tool_results_dir()
        tool_results_dir.mkdir(parents=True, exist_ok=True)

        tool_result_id = uuid.uuid4().hex
        filename = f"tool_result_{tool_result_id}.json"
        path = tool_results_dir / filename

        payload = {
            "tool_result_id": tool_result_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "calling_agent": calling_agent,
            "scope_id": scope_id,
            "tool_result": tool_result.model_dump() if hasattr(tool_result, "model_dump") else str(tool_result),
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        # Best-effort retention to avoid unbounded growth.
        _prune_tool_results_dir(tool_results_dir=tool_results_dir, max_files=max_files, max_age_hours=max_age_hours)

        return {"tool_result_id": tool_result_id, "path": str(path)}
    except Exception as e:
        logger.warning(f"[tool_result_handler] Failed to persist tool result artifact: {e}")
        return None


class ToolResultHandler(ControlNode):
    def __init__(self, name, blackboard, agent_registry, tool_registry):
        super().__init__(name, blackboard, agent_registry, tool_registry)

    def action_handler(self, message):
        """
        Main entry point - determines if this is a tool result or agent result.
        This is called by the delegator for agent results.
        For tool results, tool_caller should call process_tool_result_direct() instead.
        """
        # This should only be called for agent results now
        # Tool results should use process_tool_result_direct()
        current_context = self.blackboard.get_current_call_context()

        if current_context:
            # current_context is a tuple: (calling_agent, called_agent, scope_id)
            calling_agent, called_agent, scope_id = current_context
            # Agent results are stored in GLOBAL blackboard by Agent._handle_flow_control()
            # get_state_value searches from current scope down to global, so it will find it
            result = self.blackboard.get_state_value(f"{called_agent}_result")
            # If there is no agent result and no scope-level `result`, do NOT pop the call context.
            # This situation can happen if this handler is reached via state_map after a tool call.
            # Popping the root scope will corrupt the manager loop and can cause attempts to route
            # back to the manager name as if it were an agent.
            try:
                scope_result = self.blackboard.get_state_value("result")
            except Exception as e:
                logger.error("[%s] Could not read scope result from blackboard: %s", self.name, e)
                logger.debug("[%s] Could not read scope result from blackboard", self.name, exc_info=True)
                raise

            if result is None and scope_result is None:
                # Route back to a real agent without mutating scopes.
                # Prefer calling_agent from the call context, otherwise fall back to called_agent.
                nxt = calling_agent if isinstance(calling_agent, str) else None
                if not isinstance(nxt, str) or not nxt.strip() or not self.agent_registry.get_agent_config(nxt.strip()):
                    nxt = called_agent if isinstance(called_agent, str) else None
                self.blackboard.update_state_value("last_agent", self.name)
                self.blackboard.update_state_value("next_agent", nxt)
                logger.warning(
                    "[%s] action_handler reached with call_context=%r but no agent result; "
                    "skipping pop_call_context and returning to %r",
                    self.name,
                    current_context,
                    nxt,
                )
                return

            self._process_agent_result(result, current_context)
        else:
            logger.warning(f"[{self.name}] action_handler called with no call context")
            raise ValueError(f"[{self.name}] action_handler requires active call context.")
    
    def process_tool_result_direct(self, tool_result: ToolResult | None = None):
        """
        Called directly by tool_caller after a tool executes.
        Handles tool results without needing to check call context.
        """
        self.blackboard.update_state_value('next_agent', None)

        if tool_result:
            self._process_tool_result(tool_result)
        else:
            logger.error(f"[{self.name}] process_tool_result_direct called but no tool_result found")
            raise ValueError(f"[{self.name}] process_tool_result_direct requires non-null tool_result.")


    def _process_tool_result(self, tool_result):
        """
        Process a tool result and record it in the calling agent's history.
        This is only called for actual tool calls (not agent calls).
        """
        # NOTE: tool_result.data can include very large payloads (e.g., base64 screenshots from MCP).
        # Avoid logging the full object to keep logs readable and prevent huge terminal output.
        try:
            data = getattr(tool_result, "data", None) if tool_result else None
            backend = data.get("backend") if isinstance(data, dict) else None
            server_id = data.get("server_id") if isinstance(data, dict) else None
            mcp_tool = data.get("mcp_tool_name") if isinstance(data, dict) else None
            content_preview = getattr(tool_result, "content", "") or ""
            attachments = []
            if isinstance(data, dict) and isinstance(data.get("attachments"), list):
                attachments = data.get("attachments") or []
            logger.debug(
                "Processing tool result: result_type=%r backend=%r server_id=%r mcp_tool=%r attachments=%d content_preview=%r",
                getattr(tool_result, "result_type", None),
                backend,
                server_id,
                mcp_tool,
                len(attachments),
                content_preview,
            )
        except Exception:
            logger.debug("Processing tool result (preview failed)")

        # Convert the tool result into a summarized format for the data field.
        tool_result_msg = DI.data_conversion_module.convert(tool_result, "summary")

        # Content for prompts/history: use the plain text content string, never JSON.
        # The structured data stays in message.data for code consumers.
        content_str = str(getattr(tool_result, "content", "") or "")
        result_type = str(getattr(tool_result, "result_type", "") or "")
        # ask_user replies are user instructions. They supersede the original task.
        if result_type == "ask_user_response" and content_str.strip():
            content_str = (
                f"User responded with: {content_str.strip()}\n"
                "Obey this reply; it supersedes the original task. If it ends or redirects the task, return_control."
            )
        if not content_str.strip():
            if isinstance(tool_result_msg, dict):
                content_str = str(tool_result_msg.get("tool_result") or tool_result_msg.get("content") or "")
        logger.debug("Tool result content: %s", content_str[:500])

        # For tool calls, use canonical runtime fields only.
        calling_agent = self.blackboard.get_state_value("calling_agent")
        if not isinstance(calling_agent, str) or not calling_agent.strip():
            raise ValueError(f"[{self.name}] Missing calling_agent while processing tool result.")
        calling_agent = calling_agent.strip()
        selected_tool = self.blackboard.get_state_value("action")
        if not isinstance(selected_tool, str) or not selected_tool.strip():
            raise ValueError(f"[{self.name}] Missing action while processing tool result.")
        selected_tool = selected_tool.strip()
        scope_id = None
        try:
            scope_id = self.blackboard.get_current_scope_id()
        except Exception:
            logger.debug("[%s] Could not read current scope id", self.name, exc_info=True)
            scope_id = None
        
        # Attachments: allow tools (including MCP) to surface image/file outputs.
        attachments = []
        try:
            if isinstance(getattr(tool_result, "data", None), dict):
                atts = tool_result.data.get("attachments")
                if isinstance(atts, list):
                    attachments = [a for a in atts if isinstance(a, dict)]
        except Exception:
            logger.debug("[%s] Could not read attachments from tool_result", self.name, exc_info=True)
            attachments = []

        # Persist the full tool result payload to disk (artifact store) so agents can
        # later retrieve it without bloating their prompt context.
        artifact_ref = None
        try:
            persist_enabled = bool(get_flag(self.blackboard, "persist_tool_result_artifacts", True))
            max_files = int(get_flag(self.blackboard, "tool_result_artifact_max_files", 500) or 500)
            max_age_hours = int(get_flag(self.blackboard, "tool_result_artifact_max_age_hours", 72) or 72)
            if persist_enabled:
                artifact_ref = _persist_tool_result_artifact(
                    tool_result=tool_result,
                    calling_agent=calling_agent,
                    scope_id=scope_id,
                    max_files=max_files,
                    max_age_hours=max_age_hours,
                )
        except Exception:
            logger.debug("[%s] Failed to read artifact persistence flags", self.name, exc_info=True)
            artifact_ref = None

        # Create a message object with the processed tool result
        # scope_id will be auto-tagged with current scope by add_msg
        metadata = {}
        if attachments:
            metadata["attachments"] = attachments
        if artifact_ref:
            metadata.update(artifact_ref)

        message = Message(
            data_type="tool_result",
            sub_data_type=[tool_result.result_type] if getattr(tool_result, "result_type", None) else [],
            sender="tool",  # Generic sender for tools
            receiver=calling_agent,
            content=content_str,
            data=tool_result_msg,
            metadata=metadata or None,
        )

        # Store processed tool result in blackboard
        # This will auto-tag with the current scope_id
        self.blackboard.add_msg(message)
        self._update_latest_playwright_snapshot_state(message=message, tool_result=tool_result)

        # Record tool result in execution trace (if enabled).
        try:
            recorder = self.blackboard.get_state_value("execution_trace_recorder")
            if recorder is not None:
                snapshot = self.blackboard.get_state_value("playwright_latest_snapshot")
                page_url = snapshot.get("url", "") if isinstance(snapshot, dict) else ""
                # Extract selectors from tool arguments for invariant detection.
                tool_args = self.blackboard.get_state_value("tool_arguments")
                selectors = []
                if isinstance(tool_args, dict):
                    args_inner = tool_args.get("arguments") or tool_args
                    for key in ("ref", "selector", "element", "xpath"):
                        val = args_inner.get(key)
                        if isinstance(val, str) and val.strip():
                            selectors.append(val.strip())
                recorder.record_tool_result(
                    tool_name=selected_tool,
                    result_type=str(getattr(tool_result, "result_type", "") or ""),
                    content_preview=content_str[:500],
                    timing_ms=self.blackboard.get_state_value("last_tool_timing_ms"),
                    page_url=page_url,
                    selectors_used=selectors or None,
                )
        except Exception:
            logger.debug("[%s] Failed to record tool result in execution trace", self.name, exc_info=True)

        # Emit a progress fact for UI (short, curated later).
        try:
            preview = ""
            try:
                if isinstance(tool_result_msg, dict):
                    preview = str(tool_result_msg.get("tool_result") or "")
            except Exception:
                logger.debug("[%s] Could not extract tool_result preview", self.name, exc_info=True)
                preview = ""

            DI.event_hub.publish(
                Message(
                    sender=self.name,
                    receiver=None,
                    event_topic="agent_progress_fact",
                    data={
                        "kind": "tool_result",
                        "agent": calling_agent,
                        "tool": selected_tool,
                        "result_type": getattr(tool_result, "result_type", None),
                        "tool_result_id": (artifact_ref or {}).get("tool_result_id") if isinstance(artifact_ref, dict) else None,
                        "preview": preview,
                    },
                )
            )
        except Exception:
            logger.debug("[%s] Failed to publish tool_result progress fact", self.name, exc_info=True)

        # Update pipeline state with the latest tool_result reference.
        try:
            set_last_tool_result_ref(
                self.blackboard,
                artifact_ref if isinstance(artifact_ref, dict) else None,
                meta={
                    "tool_name": selected_tool,
                    "result_type": getattr(tool_result, "result_type", None),
                    "calling_agent": calling_agent,
                },
            )
        except Exception:
            logger.debug("[%s] Failed to update last_tool_result_ref", self.name, exc_info=True)

        # Update last agent to track execution
        self.blackboard.update_state_value('last_agent', self.name)

        # Keep a normalized final-answer payload ready for flows that route directly
        # from tool result -> final_answer_node without returning to a planner pass.
        result_payload = {
            "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
            "final_answer_answer": str(getattr(tool_result, "content", "") or ""),
            "final_answer_what_was_done": "",
            "final_answer_interesting_info": "",
            "final_answer_sources": [],
        }
        data_obj = getattr(tool_result, "data", None)
        if isinstance(data_obj, dict):
            if any(
                key in data_obj
                for key in (
                    "final_answer_answer",
                    "final_answer_task",
                    "final_answer_what_was_done",
                    "final_answer_interesting_info",
                    "final_answer_sources",
                    "answer",
                    "summary",
                )
            ):
                result_payload = data_obj
        self.blackboard.update_state_value("result", result_payload)

        # ------------------------------------------------------------
        # Tool pipeline (configurable): after-tool hooks (auto-scan, tab-follow, etc.)
        # ------------------------------------------------------------
        # Update dynamic tool scope state (visible + dynamic allow) using this result.
        routed_for_approval = False
        try:
            routed_for_approval = self._maybe_route_tool_approval_node(
                selected_tool=selected_tool,
                tool_result=tool_result,
                calling_agent=calling_agent,
            )
            self._update_dynamic_tool_scope(
                selected_tool=selected_tool,
                tool_result=tool_result,
                skip_install_apply=bool(routed_for_approval),
            )
        except Exception as e:
            logger.error("[%s] Failed to update dynamic tool scope: %s", self.name, e)
            logger.debug("[%s] dynamic tool scope update exception details", self.name, exc_info=True)

        # Normalize non-conforming tool errors before any routing decisions.
        self._normalize_nonconforming_tool_error(tool_result=tool_result)

        # Fail closed for clearance-dependent failures (ask_user / approval-gated tools).
        if self._maybe_abort_task_for_clearance_error(selected_tool=selected_tool, tool_result=tool_result):
            clear_pending_tool(self.blackboard)
            return

        # Explicit approval routing after install_tool (if manager includes tool_approve_node).
        if routed_for_approval:
            clear_pending_tool(self.blackboard)
            return

        # Canonical behavior: always defer routing to delegator/state_map via tool_return_router.
        self.blackboard.update_state_value("next_agent", None)
        logger.debug("[%s] tool_result_handler defers routing to delegator/state_map.", self.name)

        clear_pending_tool(self.blackboard)
        
        # DO NOT pop call context - tool calls stay in the same scope

    def _update_latest_playwright_snapshot_state(self, *, message: Message, tool_result) -> None:
        """
        Keep exactly one planner-visible snapshot card: the latest one.
        Older snapshot cards are hidden from context/history to prevent stale-ref usage.
        """
        try:
            data = getattr(tool_result, "data", None)
            if not isinstance(data, dict):
                return
            snapshot_id = str(data.get("snapshot_id") or "").strip()
            elements = data.get("actionable_elements")
            if not snapshot_id or not isinstance(elements, list):
                return

            snapshot_card = {
                "snapshot_id": snapshot_id,
                "url": data.get("snapshot_url") or data.get("url"),
                "title": data.get("snapshot_title"),
                "actionable_elements": elements,
                "actionable_total": int(data.get("actionable_total", len(elements)) or len(elements)),
                "truncated": bool(data.get("truncated", False)),
                "hidden_count": int(data.get("hidden_count", 0) or 0),
            }

            # If this is a modal scan result, persist the full element map separately
            # so normal viewport snapshots don't overwrite it.
            result_type = str(getattr(tool_result, "result_type", "") or "")
            if result_type == "web_modal_scan":
                all_elements = data.get("all_elements") or elements
                self.blackboard.update_state_value(
                    "playwright_modal_map",
                    self._format_modal_map(all_elements),
                )

            self.blackboard.update_state_value("playwright_latest_snapshot", snapshot_card)
            self.blackboard.update_state_value("playwright_latest_snapshot_id", snapshot_id)
            self.blackboard.update_state_value(
                "playwright_latest_snapshot_summary",
                self._format_snapshot_card(snapshot_card),
            )
            # Tools run outside the manager's blackboard — web_fill_ref /
            # web_click_ref_snapshot resolve ref -> element label from the
            # global blackboard (the tool-visible layer, same bridge the
            # bluesky tools use for their ref maps).
            DI.global_blackboard.update_state_value("playwright_latest_snapshot", snapshot_card)
        except Exception:
            logger.debug(
                "[%s] Failed to update latest playwright snapshot state",
                self.name,
                exc_info=True,
            )

    @staticmethod
    def _format_snapshot_card(card: dict) -> str:
        sid = str(card.get("snapshot_id") or "").strip()
        url = str(card.get("url") or "").strip()
        title = str(card.get("title") or "").strip()
        rows = card.get("actionable_elements")
        if not isinstance(rows, list):
            rows = []
        lines = ["Latest snapshot:"]
        if url:
            lines.append(f"- url: {url}")
        # Filter: only show form controls and buttons. Links are noise.
        SHOW_ROLES = {"input", "textarea", "checkbox", "radio", "option", "button"}
        relevant = [
            r for r in rows[:200]
            if isinstance(r, dict) and str(r.get("role") or "").strip().lower() in SHOW_ROLES
        ]
        if relevant:
            lines.append(f"- elements:")
            for r in relevant:
                ref = str(r.get("ref") or "").strip()
                role = str(r.get("role") or "").strip()
                text = str(r.get("text") or "").strip()
                source = str(r.get("source") or "").strip().lower()
                x = r.get("x")
                y = r.get("y")
                if ref:
                    src = f" source={source}" if source else ""
                    lines.append(f"  - {ref} [{role or 'unknown'}] {text or '(no label)'}{src}")
                    continue
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    src = f" source={source}" if source else " source=dom"
                    lines.append(
                        f"  - (coords:{int(round(float(x)))},{int(round(float(y)))}) "
                        f"[{role or 'input'}] {text or '(no label)'}{src}"
                    )
        return "\n".join(lines)

    @staticmethod
    def _format_modal_map(elements: list) -> str:
        """Format modal scan elements as a compact ref map for the planner."""
        if not elements:
            return ""
        grouped: dict[str, list[dict]] = {}
        for el in elements:
            if not isinstance(el, dict):
                continue
            role = str(el.get("role") or "unknown").strip().lower()
            grouped.setdefault(role, []).append(el)

        lines = [f"Modal map ({len(elements)} elements):"]
        for role in ["radio", "checkbox", "option", "input", "button", "tab", "menuitem"]:
            items = grouped.pop(role, [])
            if items:
                lines.append(f"  {role} ({len(items)}):")
                for el in items:
                    ref = str(el.get("ref") or "").strip()
                    text = str(el.get("text") or "(no label)").strip()
                    checked = el.get("checked", False)
                    mark = " [SELECTED]" if checked else ""
                    if ref:
                        lines.append(f"    [{ref}] {text}{mark}")
        for role, items in grouped.items():
            if items:
                lines.append(f"  {role} ({len(items)}):")
                for el in items:
                    ref = str(el.get("ref") or "").strip()
                    text = str(el.get("text") or "(no label)").strip()
                    if ref:
                        lines.append(f"    [{ref}] {text}")
        return "\n".join(lines)

    def _maybe_abort_task_for_clearance_error(self, *, selected_tool: str | None, tool_result) -> bool:
        if getattr(tool_result, "result_type", None) != "error":
            return False

        data = getattr(tool_result, "data", None)
        if not isinstance(data, dict):
            raise ValueError("Tool error contract violation: tool_result.data must be a dict for result_type='error'.")

        # First-class policy marker from the tool itself.
        if str(data.get("abort_policy") or "").strip().lower() == "abort_task":
            reason = str(data.get("error_code") or "clearance_required_error")
            self.blackboard.update_state_value("error", True)
            self.blackboard.update_state_value(
                "error_message",
                f"Task aborted due to fail-closed tool policy ({reason}) from '{selected_tool}'.",
            )
            # Populate final_answer_* so manager_exit/response_formatter can
            # report the failure truthfully instead of hallucinating success
            # from upstream context.
            human_reason = {
                "approval_timeout": "Approval was not received in time; nothing was changed.",
                "approval_denied": "You declined the action; nothing was changed.",
                "clearance_required_error": "The action was blocked by policy; nothing was changed.",
            }.get(reason, f"The action was aborted ({reason}); nothing was changed.")
            self.blackboard.update_state_value("final_answer_answer", human_reason)
            self.blackboard.update_state_value(
                "final_answer_what_was_done",
                f"Attempted '{selected_tool}' but the action did not execute: {reason}.",
            )
            self.blackboard.update_state_value("next_agent", None)
            logger.warning(
                "[%s] Aborting task due to fail-closed tool policy. tool=%r reason=%r",
                self.name,
                selected_tool,
                reason,
            )
            return True

        return False

    def _normalize_nonconforming_tool_error(self, *, tool_result) -> None:
        """Rewrite a contract-violating error result into a conforming,
        RECOVERABLE abort_tool shape in place. Never aborts the manager —
        the planner sees one failed tool call and decides what to do next."""
        valid, reason = validate_tool_error_contract(tool_result)
        if valid:
            return
        # Non-conforming error responses from external/third-party tools are common
        # (e.g. scrape_url returning data=null when a URL is paywalled and
        # the playwright fallback isn't installed; one_shot_tool_runner
        # returning non-dict). The right behavior is RECOVERABLE — surface
        # the error to the planner so it can try a different URL or
        # continue with what it has. The previous implementation set
        # ``blackboard.error = True`` which killed the entire manager
        # loop on a single bad URL, producing user-facing messages like
        # "the search process encountered an error and could not extract
        # meaningful content from the sources."
        #
        # Normalize the malformed error into a conforming abort_tool
        # shape (data: dict, recoverable) so downstream code paths see a
        # valid error contract and the planner sees it as a single failed
        # tool call (not a fatal manager state).
        logger.warning(
            "[%s] Tool error response does not conform to contract (%s); "
            "normalizing to recoverable abort_tool for planner visibility.",
            self.name, reason,
        )
        error_content = str(getattr(tool_result, "content", "") or "Tool returned a non-conforming error.")
        try:
            tool_result.data = {
                "error_code": "tool_error_contract_violation",
                "abort_policy": "abort_tool",
                "retryable": False,
                "user_visible": False,
                "details": {"original_reason": reason},
            }
        except Exception:
            logger.debug(
                "[%s] Failed to normalize tool_result.data — letting it flow as-is",
                self.name, exc_info=True,
            )

    def _update_dynamic_tool_scope(
        self,
        *,
        selected_tool: str | None,
        tool_result,
        skip_install_apply: bool = False,
    ) -> None:
        """
        Runtime-scoped tool visibility/allowlist updates.
        - keep recently used tools visible
        - when install_tool succeeds, add installed MCP tool to dynamic_allowed_tools
        """
        namespaced = None
        if selected_tool == "install_tool" and not skip_install_apply:
            try:
                data = getattr(tool_result, "data", None)
                if isinstance(data, dict):
                    tn = data.get("tool_name")
                    if isinstance(tn, str) and tn.strip():
                        namespaced = tn.strip()
            except Exception as e:
                logger.debug("[%s] Failed to parse install tool namespaced name: %s", self.name, e, exc_info=True)
                namespaced = None

        ToolScopeStateManager(self.blackboard).record_tool_result(
            selected_tool=selected_tool,
            installed_tool_name=namespaced,
            skip_install_apply=skip_install_apply,
        )

    def _maybe_route_tool_approval_node(self, *, selected_tool: str | None, tool_result, calling_agent: str | None) -> bool:
        if selected_tool != "install_tool":
            return False
        try:
            node = self.agent_registry.get_agent_instance("tool_approve_node")
        except Exception as e:
            logger.debug("[%s] tool_approve_node lookup failed: %s", self.name, e)
            node = None
        if node is None:
            return False

        namespaced = None
        server_id = None
        mcp_tool_name = None
        try:
            data = getattr(tool_result, "data", None)
            if isinstance(data, dict):
                tn = data.get("tool_name")
                if isinstance(tn, str) and tn.strip():
                    namespaced = tn.strip()
                sid = data.get("server_id")
                if isinstance(sid, str) and sid.strip():
                    server_id = sid.strip()
                tname = data.get("mcp_tool_name")
                if isinstance(tname, str) and tname.strip():
                    mcp_tool_name = tname.strip()
        except Exception as e:
            logger.debug("[%s] Failed to parse install_tool result payload: %s", self.name, e)

        if not isinstance(namespaced, str) or not namespaced.startswith("mcp::"):
            return False

        self.blackboard.update_state_value(
            "pending_tool_approval",
            {
                "tool_name": namespaced,
                "server_id": server_id,
                "mcp_tool_name": mcp_tool_name,
                "calling_agent": calling_agent,
                "source_tool": selected_tool,
            },
        )
        self.blackboard.update_state_value("next_agent", "tool_approve_node")
        return True

    def _process_agent_result(self, agent_result, current_context):
        """Process an agent result and record it in the calling agent's history."""
        # current_context is a tuple: (calling_agent, called_agent, scope_id)
        calling_agent, called_agent, scope_id = current_context if current_context else (None, None, None)
        logger.debug(f"Processing agent response from {called_agent}")

        # Capture callee-selected next_agent BEFORE popping scope.
        callee_next_agent = None
        try:
            callee_next_agent = self.blackboard.get_state_value("next_agent")
        except Exception:
            logger.debug("[%s] Could not read callee next_agent from blackboard", self.name, exc_info=True)
            callee_next_agent = None

        # IMPORTANT: Retrieve 'result' from the current scope BEFORE popping
        # This allows any agent in the flow (not just the originally called agent) to set the result
        scope_result = self.blackboard.get_state_value("result")
        
        # Use scope_result if available, otherwise fall back to agent_result parameter
        final_result = scope_result if scope_result is not None else agent_result
        
        if scope_result is not None:
            logger.debug(f"Using scope-based result instead of agent_result parameter")

        # Convert final_result to human-readable content for Message.
        # Prefer the answer text over raw JSON to avoid escape-nesting.
        if isinstance(final_result, dict):
            answer = str(final_result.get("final_answer_answer") or "").strip()
            if answer:
                content_str = answer
            else:
                content_str = json.dumps(final_result, indent=2)
        else:
            content_str = str(final_result) if final_result else ""

        # Create a message object with the agent response
        message = Message(
            data_type="agent_result",
            sender=called_agent,
            receiver=calling_agent,
            content=content_str,
            data=final_result  # Use final_result (scope-based if available)
        )

        # Pop the call context so we're back in the parent scope
        self.blackboard.pop_call_context()
        
        # NOW add the message to the parent scope (so planner will see it in history)
        self.blackboard.add_msg(message)

        # Update last agent to track execution (in parent scope)
        self.blackboard.update_state_value('last_agent', self.name)
        
        # Set next_agent in the parent scope (so delegator will see it).
        # If the callee explicitly selected a next agent, respect it.
        logger.debug(f"[{self.name}] Current call context: {current_context}")
        next_agent = None
        if isinstance(callee_next_agent, str) and callee_next_agent.strip():
            # Validate that the target exists in this runtime.
            if self.agent_registry.get_agent_config(callee_next_agent.strip()):
                next_agent = callee_next_agent.strip()
            else:
                logger.warning(
                    "[%s] Callee requested next_agent '%s' but it is not available; "
                    "falling back to caller '%s'",
                    self.name,
                    callee_next_agent,
                    calling_agent,
                )
        if next_agent is None:
            next_agent = calling_agent

        self.blackboard.update_state_value("next_agent", next_agent)
        logger.debug(f"[{self.name}] Setting next_agent to: {next_agent}")
