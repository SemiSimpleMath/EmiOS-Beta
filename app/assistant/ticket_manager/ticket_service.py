# ticket_service.py
"""
Ticket Service
==============

Generic service layer for handling user responses to tickets.
Agnostic to ticket type (wellness, tool approval, calendar, etc.).

Routes call this service; this service calls TicketManager.
Domain-specific post-processing happens elsewhere (stages, handlers).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


@dataclass
class TicketResponse:
    """Result of handling a user response to a ticket."""
    ticket_id: str
    action: str  # "done", "skip", "later", "acknowledge", "willdo", "no", "accept", "dismiss"
    success: bool
    error: Optional[str] = None
    snooze_until: Optional[datetime] = None


class TicketService:
    """
    Handles user responses to tickets.
    
    This service is intentionally thin - it:
    1. Validates input
    2. Calls TicketManager to transition state
    3. Returns result
    
    Domain-specific logic (what happens AFTER a ticket is accepted)
    belongs in pipeline stages or dedicated handlers, not here.
    """

    def __init__(self, ticket_manager=None):
        """
        Args:
            ticket_manager: TicketManager instance. If None, gets from DI.
        """
        self._ticket_manager = ticket_manager

    @property
    def ticket_manager(self):
        if self._ticket_manager is None:
            from app.assistant.ServiceLocator.service_locator import DI
            self._ticket_manager = DI.ticket_manager
        return self._ticket_manager

    # Actions whose free text IS the answer: the user typed the reply itself, so it
    # stands alone with no button name in front of it (the ask_user contract — the
    # calling agent receives the user's words verbatim). Keyed on the action token,
    # never on what the button is called.
    TEXT_IS_THE_ANSWER = frozenset({"answer"})

    # Map actions to state transitions
    ACTION_TO_STATE = {
        # Acceptance actions
        "done": "accepted",
        "accept": "accepted",
        "acknowledge": "accepted",
        "willdo": "accepted",
        "answer": "accepted",  # ask_user — text-bearing acceptance
        # Dismissal actions
        "skip": "dismissed",
        "dismiss": "dismissed",
        "no": "dismissed",
        # Snooze action
        "later": "snoozed",
        # Close (X button) — silently expire, no user opinion recorded
        "close": "expired",
    }

    def respond(
        self,
        ticket_id: str,
        action: str,
        user_text: Optional[str] = None,
        snooze_minutes: int = 30,
        label: Optional[str] = None,
    ) -> TicketResponse:
        """
        Handle a user response to a ticket.

        Args:
            ticket_id: The ticket ID
            action: One of:
                - Activity layout: "done", "skip", "later"
                - Advice layout: "acknowledge", "willdo", "no", "later"
                - Tool approval layout: "accept", "dismiss"
            user_text: Optional user-provided text/explanation
            snooze_minutes: Minutes to snooze (only used if action="later")
            label: The text of the button the user actually pressed, passed through
                verbatim. The surface that rendered the button is the only place that
                knows what it said — the same action token reads "Acknowledge" in the
                advice layout and "OK" in the notify layout, and the wording is expected
                to change. Absent (a surface with no buttons), the action token itself
                is recorded; we never translate a token into prose about intent.

        Returns:
            TicketResponse with success/failure info
        """
        # Validate
        if not ticket_id:
            return TicketResponse(
                ticket_id=ticket_id or "",
                action=action,
                success=False,
                error="ticket_id required",
            )

        if action not in self.ACTION_TO_STATE:
            return TicketResponse(
                ticket_id=ticket_id,
                action=action,
                success=False,
                error=f"unknown action '{action}'",
            )

        manager = self.ticket_manager

        # Get ticket to verify it exists
        ticket = manager.get_ticket_by_id(ticket_id)
        if not ticket:
            return TicketResponse(
                ticket_id=ticket_id,
                action=action,
                success=False,
                error="ticket not found",
            )

        # Build the effective text: what the user pressed, and what they typed.
        # Both verbatim. This replaced a table of canned descriptions that guessed at
        # intent from the action token — on 2026-09-15 that table recorded "User has
        # acknowledged this advice but has not committed to action yet" directly in
        # front of the user's own words, "the picture was already taken so this
        # is all moot", and every reader downstream got both halves.
        button = str(label or "").strip()
        user_elaboration = user_text.strip() if user_text else ""

        if action in self.TEXT_IS_THE_ANSWER:
            effective_text = user_elaboration or button or action
        elif button and user_elaboration:
            effective_text = f"{button} — {user_elaboration}"
        else:
            effective_text = user_elaboration or button or action

        # Closing is a response like any other: the ask was surfaced and the user
        # dealt with it. Recording it is what lets the node that asked receive a
        # result instead of waiting out its whole timeout.
        ticket.user_text = effective_text
        ticket.user_action = action
        manager.save_ticket(ticket)

        # Transition based on action
        success = False
        snooze_until = None
        target_state = self.ACTION_TO_STATE[action]

        if target_state == "accepted":
            success = manager.mark_accepted(ticket_id, user_text=effective_text)

        elif target_state == "snoozed":
            success = manager.mark_snoozed(
                ticket_id,
                snooze_minutes=snooze_minutes,
                user_text=effective_text,
            )
            if success:
                snooze_until = datetime.now(timezone.utc) + timedelta(minutes=snooze_minutes)

        elif target_state == "dismissed":
            success = manager.mark_dismissed(ticket_id, user_text=effective_text)

        elif target_state == "expired":
            success = manager.mark_expired(ticket_id, reason="User closed ticket (no opinion)")

        logger.info(
            "TicketService.respond: %s -> %s [%s] (success=%s)",
            ticket_id, action, target_state, success,
        )

        # Every successful response is published, closes included — a node waiting on
        # this ticket is waiting on a tool call, and "the user closed it" is that
        # call's result. (A ticket the SWEEPER expires never comes through here; that
        # branch lands its own result.)
        if success:
            self._publish_ticket_responded(ticket_id, action, target_state, effective_text)

        # Pending-directive transition: if the user accepted the ticket AND added
        # free-form text on top of the canned action prefix, that text is a
        # follow-up instruction the planner needs to act on. Move every linked
        # dayflow item (acted_on_item_ids) into state=pending_directive with the
        # directive stamped on metadata.
        if (
            success
            and target_state == "accepted"
            and user_elaboration
        ):
            self._mark_linked_items_pending_directive(
                ticket=ticket,
                directive_text=user_elaboration,
                ticket_id=ticket_id,
            )

        return TicketResponse(
            ticket_id=ticket_id,
            action=action,
            success=success,
            snooze_until=snooze_until,
        )

    @staticmethod
    def _mark_linked_items_pending_directive(
        *,
        ticket,
        directive_text: str,
        ticket_id: str,
    ) -> None:
        """Move the ticket's acted_on_item_ids into state=pending_directive.

        Called when a user accepts a ticket with non-empty extra text — that
        text is a directive the planner owes a decision on. The artifact stays
        alive (not suppressed) and shows up in active_dayflow_items with the
        directive attached.
        """
        trigger_context = getattr(ticket, "trigger_context", None) or {}
        if not isinstance(trigger_context, dict):
            return
        acted_on = trigger_context.get("acted_on_item_ids") or []
        if not isinstance(acted_on, list) or not acted_on:
            return

        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        from app.assistant.dayflow_orchestrator.state_store import (
            get_latest_dayflow_item_by_id,
        )

        for raw_id in acted_on:
            item_id = str(raw_id or "").strip()
            if not item_id:
                continue
            current = get_latest_dayflow_item_by_id(item_id)
            current_state = ""
            if isinstance(current, dict):
                meta = current.get("metadata") or {}
                if isinstance(meta, dict):
                    current_state = str(meta.get("state") or "").strip().lower()
            try:
                write_dayflow_item(
                    item_id,
                    state="pending_directive",
                    updates={
                        "directive_text": directive_text,
                        "directive_source_ticket_id": ticket_id,
                    },
                    reason=f"user directive on ticket {ticket_id}",
                    caller="ticket_service.respond",
                )
                logger.info(
                    "TicketService: marked %s pending_directive (was=%r) directive=%r",
                    item_id, current_state, directive_text[:80],
                )
            except Exception as e:
                logger.warning(
                    "TicketService: could not mark %s pending_directive: %s",
                    item_id, e,
                )

    @staticmethod
    def _publish_ticket_responded(ticket_id: str, action: str, target_state: str, user_text: str = "") -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            logger.info(
                "TicketService._publish_ticket_responded: publishing dayflow_ticket_responded "
                "for ticket=%s action=%s target_state=%s",
                ticket_id, action, target_state,
            )
            DI.event_hub.publish(Message(
                sender="ticket_service",
                event_topic="dayflow_ticket_responded",
                content=action,
                data={
                    "ticket_id": ticket_id,
                    "action": action,
                    "target_state": target_state,
                    "user_text": user_text,
                },
            ))
            logger.info(
                "TicketService._publish_ticket_responded: event published successfully.",
            )
        except Exception as e:
            logger.error(
                "TicketService._publish_ticket_responded: FAILED to publish event: %s", e,
            )
            logger.debug("publish exception details", exc_info=True)


# Module-level singleton for convenience
_service: Optional[TicketService] = None


def get_ticket_service() -> TicketService:
    """Get or create the singleton TicketService instance."""
    global _service
    if _service is None:
        _service = TicketService()
    return _service


def propose_notice_ticket(
    *,
    title: str,
    message: str,
    suggestion_type: str,
    trigger_reason: str,
    ticket_type: str = "dayflow_notify",
    valid_hours: int = 24,
    trigger_context: Optional[dict] = None,
) -> Optional[str]:
    """Mint + propose + publish a simple owner-notice ticket.

    The durable notify channel: the ticket persists in DB and the UI popup /
    pending-poll serve it whenever a client is (or becomes) live. Used by the
    delivery layer for failed-send notices and undeliverable reminders
    (2026-07-08 delivery audit D2/D3). Returns the ticket_id, or None when
    ticket infrastructure is unavailable (logged ERROR — callers treat the
    notice as best-effort and must not crash their host flow).
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.ticket_manager import get_ticket_manager

    try:
        ticket_manager = get_ticket_manager()
        ticket = ticket_manager.create_ticket(
            ticket_type=ticket_type,
            suggestion_type=suggestion_type,
            title=title,
            message=message,
            action_type="none",
            trigger_context=trigger_context or {},
            trigger_reason=trigger_reason,
            valid_hours=valid_hours,
        )
        if ticket is None:
            logger.error("[notice_ticket] create_ticket returned None (%s)", suggestion_type)
            return None
        if not ticket_manager.mark_proposed(ticket.ticket_id):
            logger.error("[notice_ticket] mark_proposed failed for %s", ticket.ticket_id)
            return None
        DI.event_hub.publish(Message(
            event_topic="proactive_suggestion", data=ticket.to_dict(),
        ))
        return str(ticket.ticket_id)
    except Exception:
        logger.error("[notice_ticket] failed to surface %s notice", suggestion_type, exc_info=True)
        return None
