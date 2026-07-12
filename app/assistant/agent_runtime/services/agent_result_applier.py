"""Agent result applier — writes LLM output to blackboard and creates audit messages.

Extracted from Agent._apply_llm_result_to_state() and _create_response_message().

Separates two concerns:
1. apply_result_to_state: write LLM output keys to blackboard (with global/append policy)
2. create_audit_message: create a Message recording what the agent produced
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


# Runtime control / scope keys owned by the manager loop, control nodes, and the
# scope machinery — never part of an agent's answer. Left unguarded, an agent's
# LLM output (a free-form agent, or a prompt-injected one summarizing untrusted
# email/web/chat) could write these into the blackboard: a forged scope_context
# then defeats check_tool_access — which reads the scope FROM the blackboard, with
# nothing re-stamping it before the tool call — and task_allowed_tools / next_agent
# would hijack tool policy and routing. The ONE legitimate case is a key an agent
# DECLARES in its own output schema (a delegator declares next_agent); those pass,
# every other reserved key from LLM output is skipped loudly.
_RESERVED_RUNTIME_KEYS: frozenset = frozenset({
    # scope / authority — the confirmed prompt-injection -> tool-escalation vector
    "scope_context", "scope_contract", "scope_contract_enforced",
    # tool policy
    "task_allowed_tools", "task_except_tools",
    "dynamic_allowed_tools", "dynamic_denied_tools", "visible_tools",
    # routing / flow control
    "next_agent", "last_agent", "result_writer", "pending_tool",
    "calling_agent", "manager_agent_steps",
    # file-capability sandbox — task-scoped restrictions ToolCaller hands to the
    # file tools; a forged null here would REMOVE the write/read sandbox entirely
    # (write_text_file only enforces when the value is a list)
    "allowed_write_files", "allowed_read_files", "task_spec",
    # misc runtime overrides
    "task_llm_params", "request_id",
})


class AgentResultApplier:
    """Applies LLM structured output to the agent's blackboard."""

    def __init__(self, agent_name: str, config: Dict[str, Any], blackboard):
        self._name = agent_name
        self._config = config
        self._blackboard = blackboard

    def apply_result_to_state(self, result_dict: dict) -> None:
        """Write LLM output keys to blackboard with global/append policy.

        Each key is written according to:
        - global_output_keys config: writes to global blackboard scope
        - append_fields config: appends to existing list instead of replacing
        """
        if not isinstance(result_dict, dict):
            return

        global_keys = self._config.get("global_output_keys", [])
        append_fields = self._config.get("append_fields", [])
        declared = self._declared_output_fields()

        for key, value in result_dict.items():
            if key in _RESERVED_RUNTIME_KEYS and key not in declared:
                # An agent's answer must not write runtime control/scope state.
                # This is the prompt-injection guard: a forged scope_context here
                # would defeat the tool authority wall (check_tool_access reads the
                # scope from the blackboard). A key the agent declares in its own
                # output schema (a delegator's next_agent) is exempt above.
                logger.error(
                    "[%s] LLM output tried to write reserved runtime/scope key %r "
                    "(not declared in its output schema) — skipped.",
                    self._name, key,
                )
                continue

            is_global = key in global_keys

            if key in append_fields:
                if is_global:
                    self._blackboard.append_global_state_value(key, value)
                else:
                    self._blackboard.append_state_value(key, value)
            else:
                if is_global:
                    self._blackboard.update_global_state_value(key, value)
                else:
                    self._blackboard.update_state_value(key, value)

    def _declared_output_fields(self) -> set:
        """Field names the agent's own output schema declares — the only reserved
        keys it may legitimately write (e.g. a delegator declares next_agent). A
        Pydantic model exposes them via model_fields; a raw JSON schema via its
        properties; a schema-less agent declares nothing, so it may write no
        reserved key at all."""
        so = self._config.get("structured_output") if isinstance(self._config, dict) else None
        if so is None:
            return set()
        model_fields = getattr(so, "model_fields", None)
        if isinstance(model_fields, dict):
            return set(model_fields.keys())
        if isinstance(so, dict):
            props = so.get("properties")
            if isinstance(props, dict):
                return set(props.keys())
        return set()

    def create_audit_message(self, result_dict: dict) -> None:
        """Create and persist a Message recording the agent's output."""
        if not isinstance(result_dict, dict):
            raise TypeError(f"[{self._name}] Expected dict in create_audit_message, got {type(result_dict)}")

        from app.assistant.utils.prompt_formatter import format_for_prompt
        result_text = format_for_prompt(result_dict)

        action = str(result_dict.get("action", "")).lower()
        is_exit_action = "exit" in action
        sub_data_type = ["result"] if is_exit_action else []

        msg = Message(
            data_type="agent_result",
            sub_data_type=sub_data_type,
            sender=self._name,
            receiver="Blackboard",
            content=f"{self._name} acted.\n{result_text}",
            data=result_dict,
        )
        self._blackboard.add_msg(msg)
