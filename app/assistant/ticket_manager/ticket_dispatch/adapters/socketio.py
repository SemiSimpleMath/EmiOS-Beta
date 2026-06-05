"""SocketIOTicketAdapter — desktop + mobile web popup.

Both the horizontal/vertical desktop chat and the mobile chat connect to
the same `master_room` socket binding. Emitting once to that room reaches
whichever client is currently active. The popup JS on each client
subscribes to the `proactive_suggestion` socket event and polls
`/api/tickets/pending` as a fallback.

This is the only adapter live today; the previous behavior lived inline
in `EmiEventRelay`.
"""
from __future__ import annotations

from typing import Any, Dict

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.ticket_manager.ticket_dispatch.base import TicketSurfaceAdapter
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class SocketIOTicketAdapter(TicketSurfaceAdapter):

    _ROOM = "master_room"

    @property
    def surface_name(self) -> str:
        return "socketio"

    def supports(self, ticket: Dict[str, Any]) -> bool:
        return True

    def dispatch(self, ticket: Dict[str, Any]) -> None:
        self._emit("proactive_suggestion", ticket)

    def notify_update(self, payload: Dict[str, Any]) -> None:
        self._emit("proactive_suggestion_update", payload)

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        from app.services.socket_manager import RoomNotBound

        socket_io = DI.socket_io
        if not socket_io:
            logger.error("Cannot emit %s: DI.socket_io is not registered", event)
            return

        def _do_emit(socket_id: str) -> None:
            socket_io.emit(event, payload, room=socket_id)
            logger.debug("Emitted %s to room_id=%r socket=%s", event, self._ROOM, socket_id[:8])

        try:
            DI.socket_manager.emit_to_room(self._ROOM, _do_emit)
        except RoomNotBound:
            logger.error("Cannot emit %s: no live socket for required room_id=%r", event, self._ROOM)
