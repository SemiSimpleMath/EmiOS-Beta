"""
ApprovalGateway
===============

Single entry point for requesting user approval before a sensitive tool executes.

The gateway chooses the approval *channel* based on the active surface:

  ui        → ticket widget (existing ticket-based blocking poll)
  telegram  → inline message + /approve or /deny reply via QuestionService
  sms       → inline SMS  + /approve or /deny reply via QuestionService
  slack     → inline Slack message + /approve or /deny reply via QuestionService
  (unknown) → falls back to ticket

Callers:
    approved, ticket_id, blocked_result = ApprovalGateway.request(
        tool_name=...,
        title=...,
        message=...,
        calling_agent=...,
        approval_reasons=...,
        scope_context=...,
        blackboard=...,
        timeout_seconds=300.0,
    )
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.assistant.utils.pydantic_classes import ScopeContext, ToolResult
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.surfaces import INLINE_APPROVAL_SURFACES, SURFACE_SLACK, SURFACE_SMS, SURFACE_TELEGRAM

logger = get_logger(__name__)

# Surfaces that support inline text-based approval (via QuestionService).
_INLINE_SURFACES = INLINE_APPROVAL_SURFACES

_APPROVE_TOKENS = {"/approve", "approve", "yes", "y", "ok"}
_DENY_TOKENS = {"/deny", "deny", "no", "n", "reject", "cancel"}


def _resolve_surface(scope_context: ScopeContext | None, blackboard: Any) -> str:
    if isinstance(scope_context, ScopeContext):
        s = str(scope_context.surface or "").strip().lower()
        if s:
            return s
    if blackboard is not None:
        s = str(blackboard.get_state_value("room_surface") or "").strip().lower()
        if s:
            return s
    return "ui"


def _resolve_reply_to(blackboard: Any) -> dict | None:
    try:
        request_id = blackboard.get_state_value("request_id") if blackboard else None
        if not isinstance(request_id, str) or not request_id.strip():
            return None
        route = DI.reply_router.get_route(request_id)
        return route if isinstance(route, dict) else None
    except Exception as e:
        logger.debug("[ApprovalGateway] Could not resolve reply_to: %s", e, exc_info=True)
        return None


def _resolve_room_id(scope_context: ScopeContext | None, blackboard: Any) -> str:
    if isinstance(scope_context, ScopeContext):
        rid = str(scope_context.room_id or "").strip()
        if rid:
            return rid
    if blackboard is not None:
        rid = str(blackboard.get_state_value("room_id") or "").strip()
        if rid:
            return rid
        scope_dict = blackboard.get_state_value("scope_context")
        if isinstance(scope_dict, dict):
            rid = str(scope_dict.get("room_id") or "").strip()
            if rid:
                return rid
    return ""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def request(
    *,
    tool_name: str,
    title: str,
    message: str,
    calling_agent: str,
    approval_reasons: list[str],
    scope_context: ScopeContext | None,
    blackboard: Any,
    timeout_seconds: float = 300.0,
) -> tuple[bool, str | None, ToolResult | None]:
    """
    Request user approval for a tool call.

    Returns:
        (approved: bool, approval_id: str | None, blocked_result: ToolResult | None)
        - If approved  → (True,  approval_id, None)
        - If denied    → (False, approval_id, ToolResult with rejection error)
        - If timeout   → (False, approval_id, ToolResult with timeout error)
    """
    surface = _resolve_surface(scope_context, blackboard)
    logger.info(
        "[ApprovalGateway] Requesting approval tool=%s surface=%s reasons=%s",
        tool_name, surface, approval_reasons,
    )

    if surface in _INLINE_SURFACES:
        return _inline_approval(
            tool_name=tool_name,
            title=title,
            scope_context=scope_context,
            blackboard=blackboard,
            timeout_seconds=timeout_seconds,
        )

    return _ticket_approval(
        tool_name=tool_name,
        title=title,
        message=message,
        calling_agent=calling_agent,
        approval_reasons=approval_reasons,
        scope_context=scope_context,
        blackboard=blackboard,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Ticket-based approval (UI)
# ---------------------------------------------------------------------------

def _ticket_approval(
    *,
    tool_name: str,
    title: str,
    message: str,
    calling_agent: str,
    approval_reasons: list[str],
    scope_context: ScopeContext | None,
    blackboard: Any,
    timeout_seconds: float,
) -> tuple[bool, str | None, ToolResult | None]:
    ticket_manager = DI.ticket_manager
    if ticket_manager is None:
        raise RuntimeError("TicketManager not available for tool approval.")

    room_id = _resolve_room_id(scope_context, blackboard)
    if not room_id:
        raise ValueError(
            f"[ApprovalGateway] Cannot raise approval ticket for '{tool_name}': "
            "no room_id available. Tool approval requires an active room session."
        )

    now = datetime.now(timezone.utc)
    ticket = ticket_manager.create_ticket(
        ticket_type="tool_approval",
        suggestion_type="tool_approval",
        title=title,
        message=message,
        action_type=f"tool_{tool_name}",
        action_params={
            "tool_name": tool_name,
            "requested_at": now.isoformat(),
            "approval_reasons": list(approval_reasons),
            "calling_agent": calling_agent,
        },
        trigger_context={
            "source": f"approval_gateway:{tool_name}",
            "calling_agent": calling_agent,
            "tool_name": tool_name,
            "time": now.isoformat(),
            "room_id": room_id,
        },
        valid_hours=1,
    )
    if not ticket:
        return (
            False,
            None,
            make_tool_error(
                error_code="approval_ticket_create_failed",
                message=f"Failed to create approval ticket for '{tool_name}'.",
                abort_policy="abort_task",
                retryable=False,
                details={"tool_name": tool_name},
            ),
        )

    ticket_id = ticket.ticket_id
    if not ticket_manager.mark_proposed(ticket_id):
        return (
            False,
            ticket_id,
            make_tool_error(
                error_code="approval_ticket_propose_failed",
                message=f"Failed to propose approval ticket '{ticket_id}' for '{tool_name}'.",
                abort_policy="abort_task",
                retryable=False,
                details={"tool_name": tool_name, "ticket_id": ticket_id},
            ),
        )

    logger.info(
        "[ApprovalGateway] Ticket approval pending tool=%s ticket_id=%s timeout=%.1fs",
        tool_name, ticket_id, timeout_seconds,
    )

    poll_interval = 1.0
    elapsed = 0.0
    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval

        current = ticket_manager.get_ticket_by_id(ticket_id)
        if current is None:
            return (
                False,
                ticket_id,
                make_tool_error(
                    error_code="approval_ticket_lost",
                    message=f"Approval ticket disappeared: {ticket_id}",
                    abort_policy="abort_task",
                    retryable=False,
                    details={"tool_name": tool_name, "ticket_id": ticket_id},
                ),
            )

        state = str(current.state or "").strip().lower()
        if state == "accepted":
            if not ticket_manager.mark_executing(ticket_id):
                return (
                    False,
                    ticket_id,
                    make_tool_error(
                        error_code="approval_ticket_state_failed",
                        message=f"Failed to mark ticket executing: {ticket_id}",
                        abort_policy="abort_task",
                        retryable=False,
                        details={"tool_name": tool_name, "ticket_id": ticket_id},
                    ),
                )
            logger.info("[ApprovalGateway] Ticket approved tool=%s ticket_id=%s", tool_name, ticket_id)
            return (True, ticket_id, None)

        if state in ("dismissed", "rejected"):
            logger.info("[ApprovalGateway] Ticket denied tool=%s ticket_id=%s state=%s", tool_name, ticket_id, state)
            return (
                False,
                ticket_id,
                make_tool_error(
                    error_code="approval_rejected",
                    message=(
                        f"DENIED: User rejected '{tool_name}'. "
                        "Abort this action — do NOT retry or work around it."
                    ),
                    abort_policy="abort_task",
                    retryable=False,
                    details={"tool_name": tool_name, "ticket_id": ticket_id, "state": state},
                ),
            )

        if state in ("expired", "failed"):
            return (
                False,
                ticket_id,
                make_tool_error(
                    error_code="approval_timeout",
                    message=f"Approval ticket expired/failed for '{tool_name}'.",
                    abort_policy="abort_task",
                    retryable=False,
                    details={"tool_name": tool_name, "ticket_id": ticket_id},
                ),
            )

    # Polling loop exhausted.
    ticket_manager.mark_expired(ticket_id)
    return (
        False,
        ticket_id,
        make_tool_error(
            error_code="approval_timeout",
            message=f"Approval timed out after {timeout_seconds:.0f}s for '{tool_name}'.",
            abort_policy="abort_task",
            retryable=False,
            details={"tool_name": tool_name, "ticket_id": ticket_id},
        ),
    )


# ---------------------------------------------------------------------------
# Inline approval (telegram / sms / slack)
# ---------------------------------------------------------------------------

def _inline_approval(
    *,
    tool_name: str,
    title: str,
    scope_context: ScopeContext | None,
    blackboard: Any,
    timeout_seconds: float,
) -> tuple[bool, str | None, ToolResult | None]:
    question_service = DI.question_service
    if question_service is None:
        raise RuntimeError("QuestionService not available for inline tool approval.")

    room_id = _resolve_room_id(scope_context, blackboard)
    reply_to = _resolve_reply_to(blackboard)
    request_id = blackboard.get_state_value("request_id") if blackboard else None

    approval_id = f"inline_approval_{uuid.uuid4().hex[:12]}"
    question_text = (
        f"{title}\n\n"
        f"Reply `/approve` to allow or `/deny` to block."
    )

    question_id = question_service.request_question(
        text=question_text,
        request_id=request_id if isinstance(request_id, str) else None,
        room_id=room_id if room_id else None,
        reply_to=reply_to,
        user_identity=None,
        timeout_seconds=timeout_seconds,
    )

    _emit_inline_question(
        question=question_text,
        question_id=question_id,
        request_id=request_id if isinstance(request_id, str) else None,
        room_id=room_id if room_id else None,
        reply_to=reply_to,
        scope_context=scope_context,
        tool_name=tool_name,
    )

    logger.info(
        "[ApprovalGateway] Inline approval pending tool=%s question_id=%s timeout=%.1fs",
        tool_name, question_id, timeout_seconds,
    )

    try:
        answer = question_service.wait_for_answer(question_id=question_id)
    except TimeoutError:
        logger.error(
            "[ApprovalGateway] Inline approval timed out tool=%s question_id=%s",
            tool_name, question_id,
        )
        return (
            False,
            approval_id,
            make_tool_error(
                error_code="approval_timeout",
                message=f"Inline approval timed out after {timeout_seconds:.0f}s for '{tool_name}'.",
                abort_policy="abort_task",
                retryable=False,
                details={"tool_name": tool_name, "question_id": question_id},
            ),
        )

    normalized = str(answer or "").strip().lower()
    if normalized in _APPROVE_TOKENS:
        logger.info("[ApprovalGateway] Inline approved tool=%s answer=%r", tool_name, answer)
        return (True, approval_id, None)

    logger.info("[ApprovalGateway] Inline denied tool=%s answer=%r", tool_name, answer)
    return (
        False,
        approval_id,
        make_tool_error(
            error_code="approval_rejected",
            message=(
                f"DENIED: User rejected '{tool_name}' inline. "
                "Abort this action — do NOT retry or work around it."
            ),
            abort_policy="abort_task",
            retryable=False,
            details={"tool_name": tool_name, "answer": answer},
        ),
    )


def _emit_inline_question(
    *,
    question: str,
    question_id: str,
    request_id: str | None,
    room_id: str | None,
    reply_to: dict | None,
    scope_context: ScopeContext | None,
    tool_name: str,
) -> None:
    surface = _resolve_surface(scope_context, None)
    channel_type = str((reply_to or {}).get("type") or "").strip().lower()

    if surface == SURFACE_SLACK or channel_type == SURFACE_SLACK:
        channel_id = str((reply_to or {}).get("channel_id") or "").strip()
        if channel_id:
            from app.assistant.lib.core_tools.slack.slack import SlackTool
            SlackTool().handle_send_message({"channel_id": channel_id, "text": question})
            return
        logger.error("[ApprovalGateway] Slack inline approval missing channel_id for tool=%s", tool_name)
        raise ValueError(f"Slack inline approval for '{tool_name}' requires reply_to.channel_id.")

    if surface == SURFACE_SMS or channel_type == "twilio_sms":
        to_number = str((reply_to or {}).get("to") or "").strip()
        from_number = str((reply_to or {}).get("from") or "").strip()
        if to_number and from_number:
            from app.services.twilio_sms import TwilioSmsService
            TwilioSmsService().send_sms(to_number=to_number, from_number=from_number, body=question)
            return
        logger.error("[ApprovalGateway] SMS inline approval missing to/from for tool=%s", tool_name)
        raise ValueError(f"SMS inline approval for '{tool_name}' requires reply_to.to and reply_to.from.")

    if surface == SURFACE_TELEGRAM or channel_type == SURFACE_TELEGRAM:
        chat_id = str((reply_to or {}).get("chat_id") or room_id or "").strip()
        if chat_id:
            from app.services.telegram_bot import TelegramBotService
            TelegramBotService().send_message(chat_id=chat_id, text=question)
            return
        logger.error("[ApprovalGateway] Telegram inline approval missing chat_id for tool=%s", tool_name)
        raise ValueError(f"Telegram inline approval for '{tool_name}' requires reply_to.chat_id or room_id.")

    logger.error("[ApprovalGateway] Unknown surface %r for inline approval tool=%s", surface, tool_name)
    raise ValueError(f"No inline approval channel for surface={surface!r} tool={tool_name!r}.")
