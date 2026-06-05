"""TicketSurfaceAdapter — abstract base for per-surface ticket delivery.

A surface adapter knows how to render a ticket in its own channel's
vocabulary (web popup, Telegram inline keyboard, Slack block kit, SMS text)
and how to push lifecycle updates so an in-flight rendering can be
refreshed when the ticket changes state on another surface.

The response side (user accepts / dismisses / snoozes) flows back through
the shared `TicketService.respond()` regardless of which surface the user
answered on; adapters only need to translate their surface-native action
into the canonical action vocabulary in `TicketService.ACTION_TO_STATE`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class TicketSurfaceAdapter(ABC):
    """One adapter per delivery channel."""

    @property
    @abstractmethod
    def surface_name(self) -> str:
        """Short identifier used in logs (e.g. 'socketio', 'telegram')."""

    @abstractmethod
    def supports(self, ticket: Dict[str, Any]) -> bool:
        """Whether this surface can render the given ticket today.

        Return False for ticket types this adapter does not know how to
        render yet. The registry skips non-supporting adapters silently.
        """

    @abstractmethod
    def dispatch(self, ticket: Dict[str, Any]) -> None:
        """Deliver `ticket` to this surface.

        Called once per ticket per surface. The ticket dict is the same
        shape as `Ticket.to_dict()` — adapters pick out whichever fields
        their channel needs.
        """

    @abstractmethod
    def notify_update(self, payload: Dict[str, Any]) -> None:
        """Signal that a ticket changed state (responded elsewhere, expired, etc.).

        Adapters that render a stateful UI (web popup, Slack message with
        buttons) should refresh or remove the rendering. Adapters that
        send fire-and-forget text (SMS) can no-op.
        """
