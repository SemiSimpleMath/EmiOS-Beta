import json
import uuid

from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import Message, ScopeContext, ToolResult, ToolMessage
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.tool_execution.tool_access_control import check_tool_access
from app.assistant.lib.tool_execution.tool_approval import (
    compute_approval_reasons,
    request_approval,
    finalize_approval_ticket,
)
from app.assistant.lib.tool_execution.mcp_tool_executor import execute_mcp_tool_call
from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI

logger = get_logger(__name__)


class ToolCaller(ControlNode):
    def __init__(self, name, blackboard, agent_registry, tool_registry):
        super().__init__(name, blackboard, agent_registry, tool_registry)

    def _resolve_room_id(self) -> str:
        """Return the active room_id from the blackboard.

        Sub-managers (called as tools) never get a dedicated `room_id` key,
        but they always inherit `scope_context` which carries the room_id from
        the originating session. Fall back to that when the direct key is absent.
        """
        rid = str(self.blackboard.get_state_value("room_id") or "").strip()
        if rid:
            return rid
        scope = self.blackboard.get_state_value("scope_context")
        if isinstance(scope, dict):
            rid = str(scope.get("room_id") or "").strip()
        return rid

    def action_handler(self, message):
        """
        Executes the selected tool or agent, creating a new execution scope
        for agent-to-agent calls.
        """
        # Read planner-selected target and normalized execution payload.
        selected_target = self.blackboard.get_state_value("action")
        payload = self.blackboard.get_state_value("tool_arguments")

        selected_target = selected_target.strip() if isinstance(selected_target, str) else None
        if not selected_target:
            logger.error(f"[{self.name}] Missing target selection from blackboard action.")
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("error_message", "missing action target")
            self.blackboard.update_state_value("error", True)
            return

        if not isinstance(payload, dict):
            msg = f"[{self.name}] tool_arguments must be a dict payload, got {type(payload)}."
            logger.error(msg)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("error_message", msg)
            self.blackboard.update_state_value("error", True)
            return

        payload_target = payload.get("target_name")
        if not isinstance(payload_target, str) or not payload_target.strip():
            msg = f"[{self.name}] tool_arguments payload missing non-empty target_name."
            logger.error(msg)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("error_message", msg)
            self.blackboard.update_state_value("error", True)
            return
        payload_target = payload_target.strip()
        if payload_target != selected_target:
            msg = (
                f"[{self.name}] target mismatch: action='{selected_target}' "
                f"but tool_arguments.target_name='{payload_target}'."
            )
            logger.error(msg)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("error_message", msg)
            self.blackboard.update_state_value("error", True)
            return

        normalized_arguments = payload.get("arguments")
        if normalized_arguments is None:
            normalized_arguments = {}
        if not isinstance(normalized_arguments, dict):
            msg = (
                f"[{self.name}] tool_arguments.arguments must be a dict for target "
                f"'{selected_target}', got {type(normalized_arguments)}."
            )
            logger.error(msg)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("error_message", msg)
            self.blackboard.update_state_value("error", True)
            return
        scope_contract_enforced = bool(self.blackboard.get_state_value("scope_contract_enforced", False))
        scope_context = self.blackboard.get_state_value("scope_context")
        if not scope_contract_enforced:
            logger.warning(
                "[%s] scope_contract_enforced=False for target '%s' — tool executing without scope enforcement.",
                self.name,
                selected_target,
            )
        if scope_contract_enforced:
            if not isinstance(scope_context, (dict, ScopeContext)):
                raise ValueError(
                    f"[{self.name}] Missing scope_context while scope_contract_enforced=true. "
                    "Tool/agent execution without scope contract is not allowed."
                )
            try:
                scope_context = ScopeContext.model_validate(scope_context)
            except Exception as e:
                logger.error("[%s] Invalid scope_context in ToolCaller: %s", self.name, e)
                logger.debug("[%s] scope_context validation exception details", self.name, exc_info=True)
                raise

        # Keep manager-local tool registry aligned with install registry so newly
        # installed MCP tools are callable in the same run without restart.
        try:
            if hasattr(self.tool_registry, "load_installed_mcp_tools"):
                self.tool_registry.load_installed_mcp_tools(enabled_only=True)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to refresh installed MCP tools: {e}")
            logger.debug("[%s] MCP refresh exception details", self.name, exc_info=True)


        self.blackboard.update_state_value("next_agent", None)

        # Task-level tool restrictions (safety net).
        try:
            task_allowed = self.blackboard.get_state_value("task_allowed_tools")
        except Exception:
            logger.debug("[%s] Could not read task_allowed_tools from blackboard", self.name, exc_info=True)
            task_allowed = None
        try:
            task_except = self.blackboard.get_state_value("task_except_tools")
        except Exception:
            logger.debug("[%s] Could not read task_except_tools from blackboard", self.name, exc_info=True)
            task_except = None

        # Manager's always_show list — fundamental tools that bypass
        # scope_contract narrowing + task restrictions (SCOPE_AUDIT Step 1.5).
        # Same bypass that tool_scope_service + tool_policy_resolver apply.
        try:
            manager_always_show = self.blackboard.get_state_value("manager_always_show")
        except Exception:
            manager_always_show = None

        # Only enforce if the selected target is a tool (not an agent/control node).
        is_known_tool = self.tool_registry.get_tool(selected_target) is not None
        if is_known_tool:
            allowed, reason = check_tool_access(
                tool_name=selected_target,
                scope_contract_enforced=scope_contract_enforced,
                scope_context=scope_context,
                task_allowed_tools=task_allowed,
                task_except_tools=task_except,
                caller_name=self.name,
                manager_always_show=manager_always_show,
            )
            if not allowed:
                logger.error(f"[{self.name}] {reason}")
                self.blackboard.update_state_value("last_agent", self.name)
                self.blackboard.update_state_value("error_message", reason)
                self.blackboard.update_state_value("error", True)
                return

        logger.info(f"[{self.name}] Executing target: '{selected_target}' with arguments: {normalized_arguments}")
        # Emit a progress fact so UI can show "Now calling X".
        try:
            calling_agent = self.blackboard.get_state_value("calling_agent")
            if not isinstance(calling_agent, str) or not calling_agent.strip():
                calling_agent = self.blackboard.get_state_value("last_agent")
            calling_agent = calling_agent.strip() if isinstance(calling_agent, str) else None

            DI.event_hub.publish(
                Message(
                    sender=self.name,
                    receiver=None,
                    event_topic="agent_progress_fact",
                    data={
                        "kind": "tool_call",
                        "agent": calling_agent,
                        "manager": getattr(getattr(self, "parent", None), "name", "") if getattr(self, "parent", None) else "",
                        "tool": selected_target,
                        "next_action": selected_target,
                    },
                )
            )
        except Exception:
            logger.debug("[%s] Failed to publish tool_call progress fact", self.name, exc_info=True)

        # --- Determine if the target is an agent, a tool, or a control node ---
        is_agent = False
        selected_config = self.tool_registry.get_tool(selected_target)
        if not selected_config:
            selected_config = self.agent_registry.get_agent_config(selected_target)
            if selected_config:
                if selected_config.get("type") == "control_node":
                    logger.info(f"[{self.name}] Transitioning to control node: {selected_target}")
                    self.blackboard.update_state_value('next_agent', selected_target)
                    self.blackboard.update_state_value("last_agent", self.name)
                    return
                else:
                    is_agent = True
                    # Fail fast with a clear dev error if an agent is configured but not instantiated.
                    if self.agent_registry.get_agent_instance(selected_target) is None:
                        msg = (
                            f"Agent '{selected_target}' is configured but not instantiated/registered in this manager runtime. "
                            f"Add it under the manager's `agents:` list (so AgentLoader registers an instance), "
                            f"or expose it via a tool wrapper if it must be callable cross-manager."
                        )
                        logger.error(f"[{self.name}] {msg}")
                        self.blackboard.update_state_value("last_agent", self.name)
                        self.blackboard.update_state_value("error_message", msg)
                        self.blackboard.update_state_value("error", True)
                        return
            else:
                error_msg = f"[{self.name}] '{selected_target}' not found in tool, agent, or manager registry."
                logger.error(error_msg)
                self.blackboard.update_state_value("last_agent", self.name)
                self.blackboard.update_state_value("error", True)
                self.blackboard.update_state_value("error_message", error_msg)
                return

        try:
            calling_agent = self.blackboard.get_state_value("calling_agent")
            if not isinstance(calling_agent, str) or not calling_agent.strip():
                raise ValueError(f"[{self.name}] Missing calling_agent in blackboard. Planner must set it before ToolCaller runs.")
            calling_agent = calling_agent.strip()

            if is_agent:
                self._execute_agent_call(calling_agent, selected_target, normalized_arguments, scope_context=scope_context)
            else:
                self._execute_tool_call(calling_agent, selected_target, selected_config, normalized_arguments, scope_context=scope_context)


        except Exception as e:
            logger.error(f"[{self.name}] Fatal error executing '{selected_target}': {e}", exc_info=True)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("error_message", f"Fatal execution error for '{selected_target}': {e}")
            self.blackboard.update_state_value("error", True)
            return

    # -----------------------------------------------------------------
    # Agent-to-agent call (scope push/pop — DO NOT reorder)
    # -----------------------------------------------------------------

    def _execute_agent_call(self, calling_agent, called_agent_name, arguments, *, scope_context):
        """Handles the logic for an agent calling another agent, including scope creation."""
        # 1. Log the agent call request within the CALLER's current scope.
        logger.info(f"[{self.name}] Logging agent call from '{calling_agent}' to '{called_agent_name}'")
        tool_request_msg = Message(
            data_type='tool_request',
            sender=calling_agent,
            content=f"Calling agent '{called_agent_name}' with arguments: {json.dumps(arguments)}."
        )
        self.blackboard.add_msg(tool_request_msg)

        # 2. Generate a new, unique ID for the CALLEE's execution scope.
        new_scope_id = f"scope_{uuid.uuid4()}"
        logger.info(f"[{self.name}] Creating new scope '{new_scope_id}' for call: {calling_agent} -> {called_agent_name}")

        # 3. Push the new scope_id onto the call stack. The Blackboard will
        #    automatically create a new local scope for state variables.
        self.blackboard.push_call_context(calling_agent, called_agent_name, new_scope_id)

        # 4. Invoke the agent. Any messages it creates will be auto-tagged
        #    with the new_scope_id by the Blackboard.
        agent_instance = self.agent_registry.get_agent_instance(called_agent_name)
        if agent_instance is None:
            msg = (
                f"Cannot invoke agent '{called_agent_name}': no instance registered in AgentRegistry. "
                f"This usually means the current manager did not load it under `agents:` "
                f"(or it was expected to be called via a tool wrapper instead)."
            )
            logger.error(f"[{self.name}] {msg}")
            # Undo the pushed call context so the manager doesn't get stuck with a leaked scope.
            try:
                self.blackboard.pop_call_context()
            except Exception as e:
                logger.error("[%s] Failed to unwind leaked call context: %s", self.name, e)
                logger.debug("[%s] leaked call context unwind exception details", self.name, exc_info=True)
            # Mark error for the manager loop.
            try:
                self.blackboard.update_state_value("error_message", msg)
                self.blackboard.update_state_value("error", True)
                self.blackboard.update_state_value("last_agent", self.name)
            except Exception as e:
                logger.error("[%s] Failed to set manager error state for missing agent: %s", self.name, e)
                logger.debug("[%s] manager error state set exception details", self.name, exc_info=True)
            return
        agent_input_msg = Message(agent_input=arguments, scope_context=scope_context)
        agent_tool_result = agent_instance.action_handler(agent_input_msg)

        # Store agent result where ToolResultHandler can find it, then process it immediately.
        # ToolResultHandler will pop the call context and return control to the calling agent.
        try:
            agent_result_payload = None
            if isinstance(agent_tool_result, ToolResult):
                agent_result_payload = agent_tool_result.data if isinstance(agent_tool_result.data, dict) else agent_tool_result.content
            elif isinstance(agent_tool_result, dict):
                agent_result_payload = agent_tool_result
            else:
                agent_result_payload = str(agent_tool_result) if agent_tool_result is not None else None

            # Store in the current scope (callee scope) so handler can retrieve it via get_state_value().
            self.blackboard.update_state_value(f"{called_agent_name}_result", agent_result_payload)
        except Exception as e:
            logger.error("[%s] Failed to persist %s_result payload: %s", self.name, called_agent_name, e)
            logger.debug("[%s] agent result persistence exception details", self.name, exc_info=True)

        tool_result_handler = self.agent_registry.get_agent_instance('tool_result_handler')
        if tool_result_handler:
            tool_result_handler.action_handler(Message())
        else:
            logger.error(f"[{self.name}] Could not find tool_result_handler for agent result handling")

    # -----------------------------------------------------------------
    # Standard tool call (no scope push — stays in caller's scope)
    # -----------------------------------------------------------------

    def _execute_tool_call(self, calling_agent, tool_name, tool_config, arguments, *, scope_context):
        """Handles the logic for calling a standard, non-agent tool."""
        logger.info(f"[{self.name}] Calling standard tool: {tool_name}")

        # Log the request in the current scope
        self.blackboard.add_msg(
            Message(
                data_type="tool_request",
                sender=calling_agent,
                content=f"Calling tool {tool_name} with arguments {json.dumps(arguments)}",
            )
        )

        # Build tool_data payload
        args_dict = arguments if isinstance(arguments, dict) else {}
        tool_data = {
            "tool_name": str(tool_name),
            "arguments": dict(args_dict),
        }
        # Add request-scoped context (best effort)
        request_context = self._build_request_context()
        if request_context:
            tool_data["request_context"] = request_context

        # Attach task/file guards at top-level (tools read these outside the "arguments" object)
        allowed_read_files = self.blackboard.get_state_value("allowed_read_files")
        allowed_write_files = self.blackboard.get_state_value("allowed_write_files")
        task_spec = self.blackboard.get_state_value("task_spec")

        if allowed_read_files is not None:
            tool_data["allowed_read_files"] = allowed_read_files
        if allowed_write_files is not None:
            tool_data["allowed_write_files"] = allowed_write_files
        if task_spec is not None:
            tool_data["task_spec"] = task_spec

        tool_message = ToolMessage(
            tool_name=tool_name,
            tool_data=tool_data,
            scope_context=scope_context,
        )

        is_mcp_tool = tool_config.get("backend") == "mcp"
        tool_instance = None
        if not is_mcp_tool:
            tool_class = tool_config.get("tool_class")
            if not tool_class:
                msg = f"Tool '{tool_name}' has no valid tool_class."
                logger.error(f"[{self.name}] {msg}")
                self.blackboard.update_state_value("last_agent", self.name)
                self.blackboard.update_state_value("error_message", msg)
                self.blackboard.update_state_value("error", True)
                return
            tool_instance = tool_class()

        approval_reasons = compute_approval_reasons(
            tool_name=tool_name,
            tool_config=tool_config if isinstance(tool_config, dict) else {},
            scope_context=scope_context if isinstance(scope_context, ScopeContext) else None,
            tool_message=tool_message,
        )
        approval_ticket_id = None
        if tool_name != "ask_user" and approval_reasons:
            approved, approval_ticket_id, blocked_result = request_approval(
                tool_name=tool_name,
                tool_message=tool_message,
                calling_agent=calling_agent,
                approval_reasons=approval_reasons,
                tool_instance=tool_instance,
                scope_context=scope_context if isinstance(scope_context, ScopeContext) else None,
                blackboard=self.blackboard,
            )
            if not approved:
                if blocked_result is None:
                    raise RuntimeError(
                        f"[{self.name}] Approval flow returned non-approved without ToolResult for '{tool_name}'."
                    )
                self.blackboard.update_state_value("last_agent", self.name)
                tool_result_handler = self.agent_registry.get_agent_instance("tool_result_handler")
                if tool_result_handler:
                    tool_result_handler.process_tool_result_direct(blocked_result)
                else:
                    logger.error(f"[{self.name}] Could not find tool_result_handler")
                return

        import time as _time
        _t0 = _time.monotonic()
        if is_mcp_tool:
            tool_result = execute_mcp_tool_call(
                tool_name=tool_name,
                tool_config=tool_config,
                arguments=args_dict or {},
                tool_registry=self.tool_registry,
            )
        else:
            tool_result = tool_instance.execute(tool_message)
        _tool_timing_ms = (_time.monotonic() - _t0) * 1000
        self.blackboard.update_state_value("last_tool_timing_ms", _tool_timing_ms)

        if isinstance(approval_ticket_id, str) and approval_ticket_id.strip():
            finalize_approval_ticket(ticket_id=approval_ticket_id, tool_result=tool_result)

        self.blackboard.update_state_value("last_agent", self.name)

        # Directly call tool_result_handler to process the result
        tool_result_handler = self.agent_registry.get_agent_instance("tool_result_handler")
        if tool_result_handler:
            tool_result_handler.process_tool_result_direct(tool_result)
        else:
            logger.error(f"[{self.name}] Could not find tool_result_handler")

    def _build_request_context(self) -> dict:
        """Assemble request-scoped context for tool_data (best effort)."""
        request_context = {}
        try:
            request_id = self.blackboard.get_request_id()
        except Exception as e:
            logger.error("[%s] Failed reading request_id from blackboard: %s", self.name, e)
            logger.debug("[%s] request_id read exception details", self.name, exc_info=True)
            request_id = None

        if isinstance(request_id, str) and request_id.strip():
            request_id = request_id.strip()
            request_context["request_id"] = request_id
            try:
                reply_to = DI.reply_router.get_route(request_id)
            except Exception as e:
                logger.error("[%s] Failed reading reply route for request_id=%s: %s", self.name, request_id, e)
                logger.debug("[%s] reply route read exception details", self.name, exc_info=True)
                reply_to = None
            if isinstance(reply_to, dict):
                request_context["reply_to"] = reply_to

        # Fallback: background/cron-triggered managers don't have a request_id
        # registered in DI.reply_router (they were spawned without an HTTP
        # request context). The MultiAgentManager entry point still stashes
        # ``inbound_reply_to`` on the blackboard if the inbound Message carried
        # one — pull from there if the router lookup didn't find anything.
        if "reply_to" not in request_context:
            bb_reply_to = self.blackboard.get_state_value("inbound_reply_to")
            if isinstance(bb_reply_to, dict) and bb_reply_to:
                request_context["reply_to"] = bb_reply_to

        room_id = self._resolve_room_id()
        if room_id:
            request_context["room_id"] = room_id

        room_speaker_external_id = self.blackboard.get_state_value("room_speaker_external_id")
        if isinstance(room_speaker_external_id, str) and room_speaker_external_id.strip():
            request_context["user_identity"] = room_speaker_external_id.strip()
        elif isinstance(request_id, str) and request_id.strip():
            request_context["user_identity"] = f"request::{request_id.strip()}"

        ask_user_timeout_cfg = self.blackboard.get_state_value("ask_user_timeout_seconds")
        if ask_user_timeout_cfg is not None:
            request_context["ask_user_timeout_seconds"] = ask_user_timeout_cfg

        approval_timeout_cfg = self.blackboard.get_state_value("approval_timeout_seconds")
        if approval_timeout_cfg is not None:
            request_context["approval_timeout_seconds"] = approval_timeout_cfg

        return request_context
