"""Agent input applier — unpacks inbound messages onto the blackboard.

Extracted from Agent._update_blackboard_state() to separate three concerns:
1. apply_agent_input: unpack message.agent_input into blackboard keys
2. apply_scope_context: validate and persist scope contract
3. capture_transport_context: extract request_id and reply_to routing
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.agent_runtime.services.agent_result_applier import _RESERVED_RUNTIME_KEYS
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext

logger = get_logger(__name__)


class AgentInputApplier:
    """Unpacks inbound message data onto the agent's blackboard."""

    def __init__(self, agent_name: str, blackboard):
        self._name = agent_name
        self._blackboard = blackboard

    def apply(self, agent, message: Message) -> None:
        """Full apply: scope → transport → agent_input. Replaces _update_blackboard_state."""
        self._blackboard.update_state_value("next_agent", None)
        self.apply_scope_context(message)
        self.capture_transport_context(agent, message)
        self.enforce_scope_contract()
        self.apply_agent_input(message)

    def apply_scope_context(self, message: Message) -> None:
        """Validate and persist scope_context from the inbound message."""
        inbound_scope = getattr(message, "scope_context", None)
        if inbound_scope is not None:
            try:
                parsed_scope = ScopeContext.model_validate(inbound_scope)
                self._blackboard.update_state_value("scope_context", parsed_scope.model_dump())
            except Exception as e:
                logger.error("[%s] Invalid inbound scope_context at agent boundary: %s", self._name, e)
                logger.debug("[%s] inbound scope_context validation exception details", self._name, exc_info=True)
                raise

    def capture_transport_context(self, agent, message: Message) -> None:
        """Extract request_id and reply_to routing from message."""
        try:
            agent._active_request_id = getattr(message, "request_id", None) or getattr(message, "id", None)
        except Exception:
            logger.debug("[%s] Could not read request_id from message", self._name, exc_info=True)
            agent._active_request_id = None
        try:
            meta = getattr(message, "metadata", None)
            agent._active_reply_to = meta.get("reply_to") if isinstance(meta, dict) else None
            logger.info("[TTS:Agent] %s _active_reply_to=%r", self._name, agent._active_reply_to)
        except Exception:
            logger.debug("[%s] Could not read reply_to from message metadata", self._name, exc_info=True)
            agent._active_reply_to = None

    def enforce_scope_contract(self) -> None:
        """Raise if scope_contract_enforced=true but no scope is set.

        No try/except-to-default: a blackboard read failure must propagate, not
        silently default enforced=False (that would skip the gate on a broken
        blackboard — fail-open). The `default=False` on the flag read is the
        legitimate 'key absent => no contract requested' case, not a swallow.
        """
        enforced = bool(self._blackboard.get_state_value("scope_contract_enforced", False))
        if enforced and self._blackboard.get_state_value("scope_context") is None:
            raise ValueError(
                f"[{self._name}] Missing scope_context while scope_contract_enforced=true. "
                "Agent execution without scope contract is not allowed."
            )

    def apply_agent_input(self, message: Message) -> None:
        """Unpack message.agent_input onto the blackboard."""
        ai = message.agent_input
        if isinstance(ai, dict):
            logger.debug("[%s] Unpacking agent_input dict with keys: %s", self._name, list(ai.keys()))
            for k, v in ai.items():
                if k in _RESERVED_RUNTIME_KEYS:
                    # Same guard as apply_result_to_state, sibling path: agent_input is
                    # INPUT (scope legitimately travels on message.scope_context, which
                    # apply_scope_context validated BEFORE this runs) — a reserved key
                    # here would overwrite the validated runtime/scope state.
                    logger.error(
                        "[%s] agent_input tried to write reserved runtime/scope key %r — skipped.",
                        self._name, k,
                    )
                    continue
                logger.debug("[%s] Setting blackboard['%s'] = %s...", self._name, k, str(v)[:100] if v else "None")
                self._blackboard.update_state_value(k, v)
        elif isinstance(ai, str):
            logger.debug("[%s] Setting agent_input as string: %s...", self._name, ai[:50])
            self._blackboard.update_state_value("agent_input", ai)
        else:
            logger.debug("[%s] No agent_input or unsupported type: %s", self._name, type(ai))
            self._blackboard.update_state_value("agent_input", None)
