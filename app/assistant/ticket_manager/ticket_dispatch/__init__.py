"""Ticket surface dispatcher — fan-out of tickets to per-surface adapters.

Each `TicketSurfaceAdapter` owns the rendering vocabulary for one delivery
channel (desktop SocketIO popup, Telegram inline keyboard, Slack block-kit,
SMS text). The registry subscribes to the event_hub topics
`proactive_suggestion` and `proactive_suggestion_update` and fans each event
out to every adapter whose `supports()` returns True for that ticket.

Adding a new surface = one new adapter file + one register() call in
initialize_system.py. Nothing else changes.
"""
from app.assistant.ticket_manager.ticket_dispatch.base import TicketSurfaceAdapter
from app.assistant.ticket_manager.ticket_dispatch.registry import (
    TicketDispatcherRegistry,
)

__all__ = ["TicketSurfaceAdapter", "TicketDispatcherRegistry"]
