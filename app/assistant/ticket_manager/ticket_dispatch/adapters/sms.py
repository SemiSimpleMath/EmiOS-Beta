"""SmsTicketAdapter — stub.

SMS delivery is on the queued roadmap but not built. Text-only channel
constrains the ticket types this surface can carry — likely ASK_USER
and binary yes/no actionable tickets only. The adapter is registered
so the dispatcher fan-out has a slot ready; `supports()` returns False
for every ticket so nothing is rendered today.

When ship: format the ticket as SMS text, send via TwilioSmsService.
Replies are parsed (yes/no/accept/dismiss/free-text-for-answer) and
fed back through `TicketService.respond()`.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.ticket_manager.ticket_dispatch.base import TicketSurfaceAdapter


class SmsTicketAdapter(TicketSurfaceAdapter):

    @property
    def surface_name(self) -> str:
        return "twilio_sms"

    def supports(self, ticket: Dict[str, Any]) -> bool:
        return False

    def dispatch(self, ticket: Dict[str, Any]) -> None:
        return

    def notify_update(self, payload: Dict[str, Any]) -> None:
        return
