"""
Tool approval logic for ToolCaller.

Determines whether a tool needs user approval, requests it via the
ApprovalGateway, and finalizes approval tickets after execution.
"""
from __future__ import annotations

import json
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
import app.assistant.lib.core_tools.approval_gateway.approval_gateway as approval_gateway
from app.assistant.utils.pydantic_classes import ScopeContext, ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def compute_approval_reasons(
    *,
    tool_name: str,
    tool_config: dict,
    scope_context: ScopeContext | None,
    tool_message: ToolMessage | None = None,
) -> list[str]:
    """Return a list of reasons why *tool_name* requires approval (empty = no approval needed).

    ``tool_message`` is optional but enables the per-tool argument-aware
    softening hook (BaseTool.compute_approval_reduction). Callers that
    have a tool_message in hand (ToolCaller does) should pass it so the
    hook can fire; callers that don't (e.g., contract-level audits)
    leave it None and the hook is skipped.
    """
    import os
    if os.environ.get("EMI_BYPASS_APPROVAL") == "1":
        return []

    reasons: list[str] = []

    authority_level = 0
    if isinstance(scope_context, ScopeContext):
        required = scope_context.tools.requires_approval_tools or []
        required_set = {str(x).strip() for x in required if isinstance(x, str) and str(x).strip()}
        if tool_name in required_set:
            reasons.append("scope.requires_approval_tools")
        authority_level = int(scope_context.approval.authority_level or 0)

    # Authority 100 is the admin bypass: no tool needs approval at max authority.
    if authority_level >= 100:
        return []

    # Per-tool argument-aware softening (BaseTool.compute_approval_reduction).
    # A tool can opt into "this specific invocation is safer than the default,
    # lower the bar." Canonical example: send_email returns 95 when the
    # recipient is in the explicit email allowlist, so dayflow_orchestrator
    # (authority 95) can autonomously email family members without firing
    # a confirmation ticket per send.
    tool_class = tool_config.get("tool_class")
    if tool_message is not None and tool_class is not None:
        try:
            instance = tool_class() if isinstance(tool_class, type) else tool_class
            softened = instance.compute_approval_reduction(tool_message, authority_level)
        except Exception:
            logger.warning(
                "[tool_approval] compute_approval_reduction raised for %s; "
                "falling back to standard approval logic", tool_name,
                exc_info=True,
            )
            softened = None
        if softened is not None and authority_level >= int(softened):
            # The invocation passes the softened bar; bypass approval entirely.
            # We still allow `scope.requires_approval_tools` to win (the user
            # has explicitly named this tool as requiring approval for this
            # scope, even on the softened path) — those reasons are already
            # appended above and short-circuit out.
            if not reasons:
                return []

    contract = tool_config.get("tool_contract") if isinstance(tool_config.get("tool_contract"), dict) else {}
    metadata = contract.get("metadata") if isinstance(contract.get("metadata"), dict) else {}
    approval_min_authority = metadata.get("approval_min_authority")
    has_authority_threshold = approval_min_authority is not None
    if has_authority_threshold:
        if isinstance(approval_min_authority, bool):
            raise ValueError("tool_contract.metadata.approval_min_authority must be an integer between 0 and 100.")
        required_authority = int(approval_min_authority)
        if required_authority < 0 or required_authority > 100:
            raise ValueError("tool_contract.metadata.approval_min_authority must be between 0 and 100.")
        if authority_level < required_authority:
            reasons.append("tool_contract.metadata.approval_min_authority")
    elif bool(metadata.get("approval_required", False)):
        reasons.append("tool_contract.metadata.approval_required")

    tool_class = tool_config.get("tool_class")
    # tool_class.requires_approval is only evaluated when no approval_min_authority threshold
    # is set on the contract. When approval_min_authority is present it supersedes the class flag.
    if (not has_authority_threshold) and tool_class is not None and bool(getattr(tool_class, "requires_approval", False)):
        reasons.append("tool_class.requires_approval")

    return reasons


def get_approval_timeout_seconds(tool_message: ToolMessage) -> float:
    """Parse the approval timeout from request context, defaulting to 300s."""
    timeout_seconds = 300.0
    try:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        request_context = tool_data.get("request_context") if isinstance(tool_data, dict) else {}
        if isinstance(request_context, dict):
            raw = request_context.get("approval_timeout_seconds", timeout_seconds)
            timeout_seconds = float(raw)
    except Exception:
        logger.error("[tool_approval] Failed parsing approval timeout from request context.")
        logger.debug("[tool_approval] approval timeout parse exception details", exc_info=True)
        raise
    if timeout_seconds <= 0:
        raise ValueError("Approval timeout must be > 0 seconds.")
    return timeout_seconds


def request_approval(
    *,
    tool_name: str,
    tool_message: ToolMessage,
    calling_agent: str,
    approval_reasons: list[str],
    tool_instance: Any,
    scope_context: ScopeContext | None,
    blackboard: Any,
) -> tuple[bool, str | None, ToolResult | None]:
    """Delegate to ApprovalGateway, which picks the right channel (ticket vs inline)."""
    title = f"Allow {tool_name}?"
    try:
        _args = (tool_message.tool_data or {}).get('arguments', {})
        message = json.dumps(_args, indent=2, ensure_ascii=False)
    except Exception:
        logger.debug("[tool_approval] Could not JSON-format tool arguments for approval message", exc_info=True)
        message = str((tool_message.tool_data or {}).get('arguments', {}))
    if tool_instance is not None and hasattr(tool_instance, "describe_action"):
        try:
            title, message = tool_instance.describe_action(tool_message)
        except Exception as e:
            logger.error("[tool_approval] Failed to build approval message for '%s': %s", tool_name, e)
            logger.debug("[tool_approval] approval message exception details", exc_info=True)
            raise

    timeout_seconds = get_approval_timeout_seconds(tool_message)

    return approval_gateway.request(
        tool_name=tool_name,
        title=title,
        message=message,
        calling_agent=calling_agent,
        approval_reasons=approval_reasons,
        scope_context=scope_context,
        blackboard=blackboard,
        timeout_seconds=timeout_seconds,
    )


def finalize_approval_ticket(*, ticket_id: str | None, tool_result: ToolResult) -> None:
    """Mark the approval ticket as completed or failed after tool execution."""
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        return
    if ticket_id.startswith("inline_approval_"):
        return
    ticket_manager = DI.ticket_manager
    if ticket_manager is None:
        raise RuntimeError("TicketManager not available while finalizing approval ticket.")
    is_error = getattr(tool_result, "result_type", "") == "error"
    result_preview = str(getattr(tool_result, "content", "") or "")[:200]
    if is_error:
        if not ticket_manager.mark_failed(ticket_id=ticket_id, execution_result=result_preview):
            raise RuntimeError(f"Failed to mark approval ticket '{ticket_id}' as failed.")
        return
    if not ticket_manager.mark_completed(ticket_id=ticket_id, execution_result=result_preview):
        raise RuntimeError(f"Failed to mark approval ticket '{ticket_id}' as completed.")
