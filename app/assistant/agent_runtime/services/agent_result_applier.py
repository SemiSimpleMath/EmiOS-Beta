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

        for key, value in result_dict.items():
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
