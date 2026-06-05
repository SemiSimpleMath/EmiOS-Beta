"""TelegramTicketAdapter — stub.

Telegram delivery is on the queued roadmap but not built. The adapter is
registered so the dispatcher fan-out has a slot ready; `supports()`
returns False for every ticket so nothing is rendered today.

When ship: render the ticket as a Telegram message with an inline
keyboard. The button payloads map to the canonical action vocabulary in
`TicketService.ACTION_TO_STATE` (accept / dismiss / snooze / answer);
the Telegram callback handler calls `TicketService.respond(ticket_id,
action)`. Telegram-specific rendering (icons, line breaks, which
ticket_types it supports) lives entirely inside this adapter.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.ticket_manager.ticket_dispatch.base import TicketSurfaceAdapter


class TelegramTicketAdapter(TicketSurfaceAdapter):

    @property
    def surface_name(self) -> str:
        return "telegram"

    def supports(self, ticket: Dict[str, Any]) -> bool:
        return False

    def dispatch(self, ticket: Dict[str, Any]) -> None:
        return

    def notify_update(self, payload: Dict[str, Any]) -> None:
        return
