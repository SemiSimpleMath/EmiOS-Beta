"""TicketDispatcherRegistry — fans out ticket events to surface adapters.

Subscribes to two event_hub topics:

  proactive_suggestion         — a freshly-proposed ticket; each adapter
                                  that `supports()` it gets `dispatch()`.
  proactive_suggestion_update  — a state change (responded, expired,
                                  dismissed) that any in-flight rendering
                                  needs to reflect; every adapter gets
                                  `notify_update()`.

If one adapter raises, the registry logs ERROR and continues to the
others — one broken surface must not block delivery to the rest.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.ticket_manager.ticket_dispatch.base import TicketSurfaceAdapter
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


class TicketDispatcherRegistry:

    def __init__(self) -> None:
        self._adapters: List[TicketSurfaceAdapter] = []

    def register(self, adapter: TicketSurfaceAdapter) -> None:
        self._adapters.append(adapter)
        logger.info("Registered ticket surface adapter: %s", adapter.surface_name)

    def subscribe_to_event_hub(self, event_hub) -> None:
        event_hub.register_event("proactive_suggestion", self._on_proactive_suggestion)
        event_hub.register_event("proactive_suggestion_update", self._on_proactive_suggestion_update)

    def _on_proactive_suggestion(self, message: Message) -> None:
        ticket = self._extract_payload(message)
        if not ticket:
            return
        for adapter in self._adapters:
            try:
                if not adapter.supports(ticket):
                    continue
                adapter.dispatch(ticket)
            except Exception as e:
                logger.error(
                    "Ticket dispatch to surface=%s failed: %s",
                    adapter.surface_name, e, exc_info=True,
                )

    def _on_proactive_suggestion_update(self, message: Message) -> None:
        payload = self._extract_payload(message)
        if not payload:
            return
        for adapter in self._adapters:
            try:
                adapter.notify_update(payload)
            except Exception as e:
                logger.error(
                    "Ticket update notify to surface=%s failed: %s",
                    adapter.surface_name, e, exc_info=True,
                )

    @staticmethod
    def _extract_payload(message: Message) -> Dict[str, Any]:
        data = message.data if hasattr(message, "data") else None
        return data if isinstance(data, dict) else {}
