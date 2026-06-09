"""Shared tool-dispatch utility for chat / master_room / dayflow tool callers.

The three tool-caller nodes all do the same thing for the actual tool /
sub-manager dispatch: read action + tool_arguments off the blackboard,
resolve as tool-or-agent, execute, place the result back, persist a tool
result row to unified_log. The only domain-specific divergence is what
happens AROUND the dispatch (master_room closes its dispatch marker,
dayflow has post_room_finalize do its own bookkeeping, basic chat does
nothing extra).

This util holds the shared dispatch and persistence; the three caller
nodes each extend ``ControlNode`` directly and call ``execute_dispatch``
for the shared piece.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

from app.assistant.lib.tool_execution.mcp_tool_executor import execute_mcp_tool_call
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeContext,
    ToolMessage,
    ToolResult,
)

logger = get_logger(__name__)


def execute_dispatch(
    *,
    name: str,
    blackboard,
    tool_registry,
    agent_registry,
) -> Any:
    """Resolve target → execute (tool or agent) → place result on
    blackboard → persist tool result. Returns the result payload.

    Caller responsibilities:
    - Set blackboard "action" (the target name) and "tool_arguments" (dict
      with "arguments" key) before calling.
    - Handle any post-execute domain-specific cleanup (e.g. closing a
      dispatch marker).

    Raises ValueError on missing/wrong-type inputs or unresolvable target.
    """
    selected_target = str(blackboard.get_state_value("action") or "").strip()
    if not selected_target:
        raise ValueError(f"[{name}] Missing action on blackboard.")

    payload = blackboard.get_state_value("tool_arguments")
    if not isinstance(payload, dict):
        raise ValueError(
            f"[{name}] tool_arguments must be a dict, got {type(payload).__name__}."
        )

    normalized_arguments = payload.get("arguments")
    if not isinstance(normalized_arguments, dict):
        normalized_arguments = {}

    scope_context = blackboard.get_state_value("scope_context")
    if isinstance(scope_context, dict):
        scope_context = ScopeContext.model_validate(scope_context)

    # Resolve target type: tool, agent (sub-manager), or missing.
    tool_config = tool_registry.get_tool(selected_target)
    is_agent = False
    if not tool_config:
        agent_config = agent_registry.get_agent_config(selected_target)
        if not agent_config:
            raise ValueError(
                f"[{name}] '{selected_target}' not found in tool or agent registry."
            )
        is_agent = True

    logger.info(
        "[%s] Executing target '%s' with arguments: %s",
        name, selected_target, normalized_arguments,
    )

    try:
        if is_agent:
            result_payload = _execute_agent(
                name=name,
                blackboard=blackboard,
                agent_registry=agent_registry,
                agent_name=selected_target,
                arguments=normalized_arguments,
                scope_context=scope_context,
            )
        else:
            result_payload = _execute_tool(
                name=name,
                blackboard=blackboard,
                tool_registry=tool_registry,
                tool_name=selected_target,
                tool_config=tool_config,
                arguments=normalized_arguments,
                scope_context=scope_context,
            )
    except Exception as e:
        logger.error(
            "[%s] Fatal error executing '%s': %s",
            name, selected_target, e, exc_info=True,
        )
        raise

    # Place result on blackboard for downstream nodes.
    blackboard.update_state_value("result", result_payload)

    # Carry research pods up the manager chain. A sub-manager's result may carry
    # pod_references (the durable findings/work behind its answer). Merge them into
    # THIS scope's running list (dedup by pod_id) so manager_exit_node surfaces them
    # in this manager's final answer too. Pods are global; this is a reference relay,
    # not a re-mint — leaf tool results have no pod_references and no-op here.
    if isinstance(result_payload, dict):
        incoming = result_payload.get("pod_references")
        if isinstance(incoming, list) and incoming:
            merged = list(blackboard.get_state_value("pod_references") or [])
            seen = {r.get("pod_id") for r in merged if isinstance(r, dict)}
            for r in incoming:
                pid = r.get("pod_id") if isinstance(r, dict) else None
                if pid and pid not in seen:
                    merged.append({"pod_id": pid, "one_liner": r.get("one_liner", "")})
                    seen.add(pid)
            blackboard.update_state_value("pod_references", merged)

    # Extract clean answer text for the response formatter.
    if isinstance(result_payload, dict):
        answer = str(result_payload.get("final_answer_answer") or "").strip()
        blackboard.update_state_value(
            "tool_result_for_formatter", answer or str(result_payload),
        )
    else:
        blackboard.update_state_value(
            "tool_result_for_formatter", str(result_payload),
        )

    # Persist raw tool result to blackboard messages + unified log.
    _persist_tool_result(
        name=name, blackboard=blackboard,
        tool_name=selected_target, result_payload=result_payload,
    )

    logger.info(
        "[%s] Execution complete for '%s', routing forward.",
        name, selected_target,
    )
    return result_payload


def _execute_agent(
    *,
    name: str,
    blackboard,
    agent_registry,
    agent_name: str,
    arguments: Dict[str, Any],
    scope_context: ScopeContext | None,
) -> Dict[str, Any] | str:
    agent_instance = agent_registry.get_agent_instance(agent_name)
    if agent_instance is None:
        raise ValueError(
            f"[{name}] Agent '{agent_name}' is configured but not instantiated. "
            "Add it under the manager's agents list."
        )

    calling_agent = str(blackboard.get_state_value("calling_agent") or "").strip()
    if not calling_agent:
        calling_agent = name

    scope_id = f"scope_{uuid.uuid4()}"
    blackboard.push_call_context(calling_agent, agent_name, scope_id)

    try:
        agent_input_msg = Message(
            agent_input=arguments, scope_context=scope_context,
        )
        agent_result = agent_instance.action_handler(agent_input_msg)
    except Exception:
        try:
            blackboard.pop_call_context()
        except Exception as pop_err:
            logger.error(
                "[%s] Failed to unwind call context: %s", name, pop_err,
            )
        raise

    result_payload: Any = None
    if isinstance(agent_result, ToolResult):
        result_payload = (
            agent_result.data
            if isinstance(agent_result.data, dict)
            else agent_result.content
        )
    elif isinstance(agent_result, dict):
        result_payload = agent_result
    else:
        result_payload = str(agent_result) if agent_result is not None else ""

    # Sub-manager may set its result at scope level — pick that up.
    scope_result = blackboard.get_state_value("result")
    if scope_result is not None:
        result_payload = scope_result

    blackboard.pop_call_context()
    return result_payload


def _execute_tool(
    *,
    name: str,
    blackboard,
    tool_registry,
    tool_name: str,
    tool_config: Dict[str, Any],
    arguments: Dict[str, Any],
    scope_context: ScopeContext | None,
) -> Dict[str, Any] | str:
    tool_data: Dict[str, Any] = {
        "tool_name": tool_name,
        "arguments": dict(arguments),
    }

    room_id = str(blackboard.get_state_value("room_id") or "").strip()
    if not room_id:
        sc = blackboard.get_state_value("scope_context")
        if isinstance(sc, dict):
            room_id = str(sc.get("room_id") or "").strip()
    if room_id:
        tool_data.setdefault("request_context", {})["room_id"] = room_id

    # Propagate the originating reply_to so user-facing tools (ask_user,
    # create_dayflow_ticket, TTS) can route replies back to the same
    # transport. MultiAgentManager stashes this as ``inbound_reply_to`` on entry.
    reply_to = blackboard.get_state_value("inbound_reply_to")
    if isinstance(reply_to, dict) and reply_to:
        tool_data.setdefault("request_context", {})["reply_to"] = reply_to

    request_id = blackboard.get_state_value("request_id")
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
            tool_registry=tool_registry,
        )
    else:
        tool_class = tool_config.get("tool_class")
        if not tool_class:
            raise ValueError(
                f"[{name}] Tool '{tool_name}' has no valid tool_class."
            )
        tool_instance = tool_class()
        tool_result = tool_instance.execute(tool_message)

    result_type = str(getattr(tool_result, "result_type", "") or "").strip()
    content = str(getattr(tool_result, "content", "") or "")
    data_obj = getattr(tool_result, "data", None)

    if isinstance(data_obj, dict) and data_obj:
        result_payload = dict(data_obj)
    else:
        result_payload = {}

    result_payload.setdefault(
        "final_answer_task", str(blackboard.get_state_value("task", "") or ""),
    )
    result_payload.setdefault("final_answer_answer", content)

    if result_type == "error":
        result_payload["error"] = True
        result_payload.setdefault("error_message", content)
        logger.warning(
            "[%s] Tool '%s' returned error: %s",
            name, tool_name, content[:200],
        )

    return result_payload


def _persist_tool_result(
    *,
    name: str,
    blackboard,
    tool_name: str,
    result_payload: Dict[str, Any] | str,
) -> None:
    """Save raw tool result to blackboard messages + unified log as
    agent_msg (not chat).
    """
    try:
        from datetime import datetime, timezone
        from app.assistant.message_manager.save_to_unified_db import save_to_unified_db
        import json

        now_utc = datetime.now(timezone.utc)
        room_id = str(blackboard.get_state_value("room_id", "") or "").strip()
        room_surface = str(blackboard.get_state_value("room_surface", "") or "").strip().lower()
        room_context_id = str(
            blackboard.get_state_value("room_context_id", "") or "main"
        ).strip() or "main"

        if isinstance(result_payload, dict):
            content = str(result_payload.get("final_answer_answer") or "")
            if not content.strip():
                content = json.dumps(result_payload, ensure_ascii=False, default=str)[:2000]
        else:
            content = str(result_payload)[:2000]

        result_msg = Message(
            data_type="agent_msg",
            sub_data_type=["tool_result"],
            sender=tool_name,
            receiver=name,
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
        blackboard.add_msg(result_msg)

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
        logger.warning("[%s] Failed to persist tool result: %s", name, e)
        logger.debug(
            "[%s] tool result persistence exception details", name, exc_info=True,
        )
