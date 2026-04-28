"""
Room tool caller node.

Executes the dispatched tool or sub-manager synchronously, places the
result on the blackboard, and always routes forward (never back to the
switchboard).  Replaces the generic tool_caller + tool_result_handler
chain for room-based managers.

The state_map determines what runs after this node (response_formatter,
action_result_normalizer, final_answer_node, etc.).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.tool_execution.mcp_tool_executor import execute_mcp_tool_call
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext, ToolMessage, ToolResult

logger = get_logger(__name__)


class RoomToolCaller(ControlNode):
    """Execute a single tool/sub-manager and route forward."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        selected_target = str(self.blackboard.get_state_value("action") or "").strip()
        if not selected_target:
            raise ValueError(f"[{self.name}] Missing action on blackboard.")

        payload = self.blackboard.get_state_value("tool_arguments")
        if not isinstance(payload, dict):
            raise ValueError(f"[{self.name}] tool_arguments must be a dict, got {type(payload).__name__}.")

        normalized_arguments = payload.get("arguments")
        if not isinstance(normalized_arguments, dict):
            normalized_arguments = {}

        scope_context = self.blackboard.get_state_value("scope_context")
        if isinstance(scope_context, dict):
            scope_context = ScopeContext.model_validate(scope_context)

        # Resolve target type: tool, agent (sub-manager), or missing.
        tool_config = self.tool_registry.get_tool(selected_target)
        is_agent = False

        if not tool_config:
            agent_config = self.agent_registry.get_agent_config(selected_target)
            if not agent_config:
                raise ValueError(f"[{self.name}] '{selected_target}' not found in tool or agent registry.")
            is_agent = True

        logger.info("[%s] Executing target '%s' with arguments: %s", self.name, selected_target, normalized_arguments)

        try:
            if is_agent:
                result_payload = self._execute_agent(selected_target, normalized_arguments, scope_context)
            else:
                result_payload = self._execute_tool(selected_target, tool_config, normalized_arguments, scope_context)
        except Exception as e:
            logger.error("[%s] Fatal error executing '%s': %s", self.name, selected_target, e, exc_info=True)
            raise

        # Place result on blackboard for downstream nodes.
        self.blackboard.update_state_value("result", result_payload)

        # Extract clean answer text for the response formatter.
        if isinstance(result_payload, dict):
            answer = str(result_payload.get("final_answer_answer") or "").strip()
            self.blackboard.update_state_value("tool_result_for_formatter", answer or str(result_payload))
        else:
            self.blackboard.update_state_value("tool_result_for_formatter", str(result_payload))

        # Persist raw tool result to blackboard messages + unified log.
        self._persist_tool_result(selected_target, result_payload)

        # Close the dayflow dispatch item created by the arguments node.
        dayflow_item_id = self.blackboard.get_state_value("_master_room_dayflow_item_id")
        if dayflow_item_id:
            self._close_dayflow_dispatch_item(dayflow_item_id, result_payload)

        self.blackboard.update_state_value("last_agent", self.name)
        logger.info("[%s] Execution complete for '%s', routing forward.", self.name, selected_target)

    # ------------------------------------------------------------------
    # Sub-manager (agent-to-agent) call
    # ------------------------------------------------------------------

    def _execute_agent(
        self,
        agent_name: str,
        arguments: Dict[str, Any],
        scope_context: ScopeContext | None,
    ) -> Dict[str, Any] | str:
        agent_instance = self.agent_registry.get_agent_instance(agent_name)
        if agent_instance is None:
            raise ValueError(
                f"[{self.name}] Agent '{agent_name}' is configured but not instantiated. "
                "Add it under the manager's agents list."
            )

        calling_agent = str(self.blackboard.get_state_value("calling_agent") or "").strip()
        if not calling_agent:
            calling_agent = self.name

        # Push scope, invoke, pop scope — all inline.
        scope_id = f"scope_{uuid.uuid4()}"
        self.blackboard.push_call_context(calling_agent, agent_name, scope_id)

        try:
            agent_input_msg = Message(agent_input=arguments, scope_context=scope_context)
            agent_result = agent_instance.action_handler(agent_input_msg)
        except Exception:
            # Unwind scope on failure.
            try:
                self.blackboard.pop_call_context()
            except Exception as pop_err:
                logger.error("[%s] Failed to unwind call context: %s", self.name, pop_err)
            raise

        # Extract result payload.
        result_payload: Any = None
        if isinstance(agent_result, ToolResult):
            result_payload = agent_result.data if isinstance(agent_result.data, dict) else agent_result.content
        elif isinstance(agent_result, dict):
            result_payload = agent_result
        else:
            result_payload = str(agent_result) if agent_result is not None else ""

        # Also check scope-level result (sub-manager may set it there).
        scope_result = self.blackboard.get_state_value("result")
        if scope_result is not None:
            result_payload = scope_result

        self.blackboard.pop_call_context()
        return result_payload

    # ------------------------------------------------------------------
    # Standard tool call
    # ------------------------------------------------------------------

    def _execute_tool(
        self,
        tool_name: str,
        tool_config: Dict[str, Any],
        arguments: Dict[str, Any],
        scope_context: ScopeContext | None,
    ) -> Dict[str, Any] | str:
        tool_data: Dict[str, Any] = {
            "tool_name": tool_name,
            "arguments": dict(arguments),
        }

        room_id = str(self.blackboard.get_state_value("room_id") or "").strip()
        if not room_id:
            sc = self.blackboard.get_state_value("scope_context")
            if isinstance(sc, dict):
                room_id = str(sc.get("room_id") or "").strip()
        if room_id:
            tool_data.setdefault("request_context", {})["room_id"] = room_id

        # Propagate the originating reply_to so user-facing tools (ask_user,
        # create_dayflow_ticket, notify_user, TTS) can route replies back to
        # the same transport. MultiAgentManager stashes this as
        # ``inbound_reply_to`` on entry; we surface it here for child tools.
        reply_to = self.blackboard.get_state_value("inbound_reply_to")
        if isinstance(reply_to, dict) and reply_to:
            tool_data.setdefault("request_context", {})["reply_to"] = reply_to

        request_id = self.blackboard.get_state_value("request_id")
        if isinstance(request_id, str) and request_id.strip():
            tool_data.setdefault("request_context", {})["request_id"] = request_id.strip()

        tool_message = ToolMessage(
            tool_name=tool_name,
            tool_data=tool_data,
            scope_context=scope_context,
        )

        is_mcp = tool_config.get("backend") == "mcp"
        if is_mcp:
            tool_result = execute_mcp_tool_call(
                tool_name=tool_name,
                tool_config=tool_config,
                arguments=arguments,
                tool_registry=self.tool_registry,
            )
        else:
            tool_class = tool_config.get("tool_class")
            if not tool_class:
                raise ValueError(f"[{self.name}] Tool '{tool_name}' has no valid tool_class.")
            tool_instance = tool_class()
            tool_result = tool_instance.execute(tool_message)

        # Normalize to a result dict, preserving error metadata.
        result_type = str(getattr(tool_result, "result_type", "") or "").strip()
        content = str(getattr(tool_result, "content", "") or "")
        data_obj = getattr(tool_result, "data", None)

        if isinstance(data_obj, dict) and data_obj:
            result_payload = dict(data_obj)
        else:
            result_payload = {}

        result_payload.setdefault("final_answer_task", str(self.blackboard.get_state_value("task", "") or ""))
        result_payload.setdefault("final_answer_answer", content)

        if result_type == "error":
            result_payload["error"] = True
            result_payload.setdefault("error_message", content)
            logger.warning("[%s] Tool '%s' returned error: %s", self.name, tool_name, content[:200])

        return result_payload

    def _persist_tool_result(self, tool_name: str, result_payload: Dict[str, Any] | str) -> None:
        """Save raw tool result to blackboard messages and unified log as agent_msg (not chat)."""
        try:
            from datetime import datetime, timezone
            from app.assistant.maintenance_manager.save_to_unified_db import save_to_unified_db
            import json

            now_utc = datetime.now(timezone.utc)
            room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
            room_surface = str(self.blackboard.get_state_value("room_surface", "") or "").strip().lower()
            room_context_id = str(self.blackboard.get_state_value("room_context_id", "") or "main").strip() or "main"

            # Build content string from result
            if isinstance(result_payload, dict):
                content = str(result_payload.get("final_answer_answer") or "")
                if not content.strip():
                    content = json.dumps(result_payload, ensure_ascii=False, default=str)[:2000]
            else:
                content = str(result_payload)[:2000]

            # 1. Add to blackboard message history (agent_msg, NOT chat).
            result_msg = Message(
                data_type="agent_msg",
                sub_data_type=["tool_result"],
                sender=tool_name,
                receiver=self.name,
                role="assistant",
                content=content,
                is_chat=False,
                timestamp=now_utc,
                room_id=room_id or None,
                room_surface=room_surface or None,
                room_context_id=room_context_id,
                room_message_direction="inbound",
                data=result_payload if isinstance(result_payload, dict) else {"content": result_payload},
            )
            self.blackboard.add_msg(result_msg)

            # 2. Persist to unified log.
            if room_id:
                source = f"room_{room_surface}" if room_surface else "room_system"
                payload = {
                    "id": result_msg.id,
                    "timestamp": now_utc,
                    "role": "assistant",
                    "message": content[:2000],
                    "source": source,
                    "room_id": room_id,
                    "room_surface": room_surface or None,
                    "room_context_id": room_context_id,
                    "room_message_direction": "inbound",
                    "speaker_name": tool_name,
                    "speaker_role": "tool",
                    "metadata_json": {"sub_data_type": ["tool_result"], "tool_name": tool_name},
                }
                save_to_unified_db([payload], source=source)

        except Exception as e:
            logger.warning("[%s] Failed to persist tool result: %s", self.name, e)
            logger.debug("[%s] tool result persistence exception details", self.name, exc_info=True)

    @staticmethod
    def _close_dayflow_dispatch_item(item_id: str, result_payload: Dict[str, Any]) -> None:
        """Stamp execution result and close the dayflow item created for this dispatch."""
        try:
            from datetime import datetime, timezone
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item

            result_summary = str(
                result_payload.get("final_answer_answer")
                or result_payload.get("content")
                or ""
            ).strip()[:500]

            write_dayflow_item(
                item_id,
                state="closed",
                updates={
                    "execution_result": result_summary or "Completed.",
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                },
                reason="master_room_dispatch_completed",
                caller="room_tool_caller",
            )
            logger.info("[room_tool_caller] closed dayflow dispatch item %s", item_id)
        except Exception as e:
            logger.warning("[room_tool_caller] failed to close dayflow dispatch item %s: %s", item_id, e)
