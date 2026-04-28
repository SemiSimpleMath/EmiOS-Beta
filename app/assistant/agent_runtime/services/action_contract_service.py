"""Action contract validation and normalization.

Extracted from Agent.py to separate the hidden mutation (normalizing
empty action_input to "{}") from the validation check.

Two distinct operations:
- normalize_action_payload: fixes up action_input for tool calls
- validate_action_payload: checks action is in the allowed set
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ActionContractService:
    """Validates and normalizes LLM action payloads for an agent."""

    def __init__(self, agent_name: str, config: Dict[str, Any], policy_resolver, action_validator):
        """
        Args:
            agent_name: Agent name for logging.
            config: Agent config dict (for action_required flag).
            policy_resolver: ToolPolicyResolver for allowed actions + is_tool_action.
            action_validator: The components.action_validator for structural validation.
        """
        self._name = agent_name
        self._config = config
        self._policy = policy_resolver
        self._validator = action_validator

    def is_required(self) -> bool:
        """Whether this agent requires an action contract in LLM output."""
        raw = self._config.get("action_required", None) if isinstance(self._config, dict) else None
        if isinstance(raw, bool):
            return raw
        return False

    def normalize_action_payload(self, result_dict: dict) -> None:
        """Fix up action_input for tool calls.

        If a tool action has empty/None action_input, normalize to "{}"
        so validation passes and argument handling happens downstream.

        Mutates result_dict in place.
        """
        if not isinstance(result_dict, dict):
            return
        action_val = result_dict.get("action")
        action_input_val = result_dict.get("action_input")
        if isinstance(action_val, str) and action_val.strip():
            action_name = action_val.strip()
            if (action_input_val is None) or (isinstance(action_input_val, str) and not action_input_val.strip()):
                if self._policy.is_tool_action(action_name):
                    result_dict["action_input"] = "{}"

    def validate_action_payload(
        self,
        result_dict: dict,
        list_action_validator: Optional[Callable] = None,
    ) -> None:
        """Validate that the action is in the allowed set.

        Must be called AFTER normalize_action_payload.

        Args:
            result_dict: The LLM output dict.
            list_action_validator: Callback for list-based action payloads.
        """
        allowed_actions = self._policy.get_allowed_actions()
        self._validator.validate(
            agent_name=self._name,
            result_dict=result_dict,
            allowed_actions=allowed_actions,
            list_action_validator=list_action_validator,
        )

    def enforce(
        self,
        result_dict: dict,
        list_action_validator: Optional[Callable] = None,
    ) -> None:
        """Normalize + validate in one call. Skips if action contract not required."""
        if not self.is_required():
            return
        self.normalize_action_payload(result_dict)
        self.validate_action_payload(result_dict, list_action_validator)
