"""SlackTicketAdapter — stub.

Slack delivery is on the queued roadmap but not built. The adapter is
registered so the dispatcher fan-out has a slot ready; `supports()`
returns False for every ticket so nothing is rendered today.

When ship: render the ticket as a Slack block-kit message with action
buttons. Action callbacks map to `TicketService.ACTION_TO_STATE` and
flow back through `TicketService.respond()`.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.ticket_manager.ticket_dispatch.base import TicketSurfaceAdapter


class SlackTicketAdapter(TicketSurfaceAdapter):

    @property
    def surface_name(self) -> str:
        return "slack"

    def supports(self, ticket: Dict[str, Any]) -> bool:
        return False

    def dispatch(self, ticket: Dict[str, Any]) -> None:
        return

    def notify_update(self, payload: Dict[str, Any]) -> None:
        return
