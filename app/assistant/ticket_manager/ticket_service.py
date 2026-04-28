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

    # Descriptive text for each action (explains user intent to agents)
    ACTION_DESCRIPTIONS = {
        # Activity layout
        "done": "User has completed this activity.",
        "skip": "User has declined this suggestion.",
        "later": "User wants to be reminded later.",
        # Advice layout  
        "acknowledge": "User has acknowledged this advice but has not committed to action yet.",
        "willdo": "User has committed to doing this right now.",
        "no": "User has indicated this suggestion is not applicable or not wanted.",
        # Tool approval layout
        "accept": "User has approved this tool action.",
        "dismiss": "User has denied this tool action.",
        # Close (X button) — not a user opinion, just clearing the UI
        "close": "User closed the ticket without expressing an opinion. Treat as if it was never shown.",
    }

    # Map actions to state transitions
    ACTION_TO_STATE = {
        # Acceptance actions
        "done": "accepted",
        "accept": "accepted",
        "acknowledge": "accepted",
        "willdo": "accepted",
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

        # Build effective text: action description + optional user elaboration
        # This gives agents full context about what the user meant
        action_desc = self.ACTION_DESCRIPTIONS.get(action, "")
        user_elaboration = user_text.strip() if user_text else ""
        
        if user_elaboration:
            # Combine: "User has acknowledged... Additional: I'll do it after lunch"
            effective_text = f"{action_desc} Additional from user: {user_elaboration}"
        else:
            effective_text = action_desc

        # For 'close' (X button), skip recording user_text/user_action so the
        # ticket looks like it was never interacted with.
        if action != "close":
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

        if success and target_state != "expired":
            self._publish_ticket_responded(ticket_id, action, target_state, effective_text)

        return TicketResponse(
            ticket_id=ticket_id,
            action=action,
            success=success,
            snooze_until=snooze_until,
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
