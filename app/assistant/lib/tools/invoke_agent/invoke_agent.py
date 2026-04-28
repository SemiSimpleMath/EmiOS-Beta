"""
invoke_agent — generic tool that runs any registered agent and returns its output.

Designed for the task IR tool_sequence executor: lets compiled tasks call
agents inline without routing through a full manager. The agent uses its
own config.yaml, prompts, and agent_form — this tool just provides the
input and captures the output.

Usage in a compiled task tool_sequence:
    {"tool": "invoke_agent", "args": {
        "agent_name": "headline_extractor",
        "agent_input": {"task": "...", "information": "..."}
    }}
"""
from __future__ import annotations

import json
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult

logger = get_logger(__name__)


class InvokeAgent(BaseTool):
    """Run a registered agent and return its structured output as a ToolResult."""

    def __init__(self):
        super().__init__("invoke_agent")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        agent_name = str(args.get("agent_name") or "").strip()
        agent_input = args.get("agent_input")

        if not agent_name:
            return make_tool_error(
                error_code="invalid_arguments",
                message="invoke_agent error: `agent_name` is required.",
                abort_policy="abort_tool",
                retryable=False,
                details={"arguments": args},
            )

        # Normalize agent_input to a dict.
        if agent_input is None:
            agent_input = {}
        elif isinstance(agent_input, str):
            agent_input = {"task": agent_input}
        elif not isinstance(agent_input, dict):
            return make_tool_error(
                error_code="invalid_arguments",
                message="invoke_agent error: `agent_input` must be a dict or string.",
                abort_policy="abort_tool",
                retryable=False,
                details={"agent_input_type": type(agent_input).__name__},
            )

        # Create the agent.
        try:
            agent = DI.agent_factory.create_agent(agent_name)
        except Exception as e:
            return make_tool_error(
                error_code="agent_not_found",
                message=f"invoke_agent error: failed to create agent '{agent_name}': {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"agent_name": agent_name},
            )

        if agent is None:
            return make_tool_error(
                error_code="agent_not_found",
                message=f"invoke_agent error: agent '{agent_name}' not found in registry.",
                abort_policy="abort_tool",
                retryable=False,
                details={"agent_name": agent_name},
            )

        # Build the message. agent_input dict gets unpacked onto the
        # agent's blackboard by AgentInputApplier.apply_agent_input().
        # Include a scope_context so agents that need resource resolution work.
        from app.assistant.utils.pydantic_classes import ScopeContext, ScopeResourcePolicy
        scope = ScopeContext(
            scope_id=f"invoke_agent::{agent_name}",
            owner_id="system",
            actor_id="invoke_agent",
            surface="internal",
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        )
        msg = Message(agent_input=agent_input, scope_context=scope)

        logger.info(
            "invoke_agent: calling agent '%s' with input keys: %s",
            agent_name, list(agent_input.keys()),
        )

        # Run the agent.
        try:
            result = agent.action_handler(msg)
        except Exception as e:
            logger.error("invoke_agent: agent '%s' raised: %s", agent_name, e, exc_info=True)
            return make_tool_error(
                error_code="agent_execution_failed",
                message=f"invoke_agent error: agent '{agent_name}' failed: {e}",
                abort_policy="abort_tool",
                retryable=True,
                details={"agent_name": agent_name},
            )

        # Extract the agent's output.
        output_data: dict[str, Any] = {}
        content = ""

        if hasattr(result, "data") and isinstance(result.data, dict):
            output_data = result.data
            content = json.dumps(output_data, indent=2, ensure_ascii=False, default=str)
        elif hasattr(result, "content") and result.content:
            content = str(result.content)
            output_data = {"content": content}
        else:
            content = str(result) if result else ""
            output_data = {"content": content}

        logger.info(
            "invoke_agent: agent '%s' returned %d output keys, content length=%d",
            agent_name, len(output_data), len(content),
        )

        return ToolResult(
            result_type="invoke_agent",
            content=content,
            data={
                "agent_name": agent_name,
                "agent_output": output_data,
            },
        )


def get_tool_class():
    return InvokeAgent
