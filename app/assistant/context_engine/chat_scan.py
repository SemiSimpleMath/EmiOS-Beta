"""
chat_scan.py — Scans recent chat for signals worth triggering context_activation.

Thin wrapper around the context_engine::chat_scan agent.
All prompt, model, and structured output config lives in:
    app/assistant/agents/context_engine/chat_scan/
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT_NAME = "context_engine::chat_scan"


# ---------------------------------------------------------------------------
# Output schema (returned to pipeline callers)
# ---------------------------------------------------------------------------

class ChatScanResult(BaseModel):
    should_activate: bool
    seeds: List[str]
    reason: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_for_activation(
    user_message: str,
    primary_user: str,
    *,
    recent_chat_context: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> ChatScanResult:
    """
    Analyse a single user message and decide if context_activation should run.

    Args:
        user_message:        The most recent user message verbatim.
        primary_user:        The name/label of the primary user (e.g. "Jukka").
        recent_chat_context: Optional recent chat text for richer signal.
        owner_id:            KG owner identifier (informational only).

    Returns:
        ChatScanResult with should_activate, seeds, and reason.

    Raises:
        Any exception from the agent framework propagates — no silent fallback.
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message, ScopeContext

    information_parts = [f"Primary user: {primary_user}"]
    if recent_chat_context:
        information_parts.append(f"Recent conversation context:\n{recent_chat_context}")

    agent = DI.agent_factory.create_agent(_AGENT_NAME)
    if agent is None:
        raise RuntimeError(f"chat_scan: agent_factory returned None for '{_AGENT_NAME}'")

    scope = ScopeContext(
        scope_id=f"scope::chat_scan::{owner_id or 'default'}",
        owner_id=owner_id or "",
        actor_id="context_engine_chat_scan",
        surface="internal",
    )

    result = agent.action_handler(
        Message(
            task=user_message,
            information="\n\n".join(information_parts),
            scope_context=scope,
        )
    )

    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"chat_scan: agent returned unexpected result type {type(result).__name__}"
        )

    return ChatScanResult(
        should_activate=bool(data.get("should_activate", False)),
        seeds=list(data.get("seeds") or []),
        reason=str(data.get("reason") or ""),
    )
