from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI

logger = get_logger(__name__)


class MusicSocketClient:
    """
    Thin adapter around your socket manager.
    Keeps the DJ logic free of socket lookup details.
    """

    def send_command(self, command: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        from app.services.socket_manager import RoomNotBound
        try:
            socket_manager = DI.socket_manager
            if socket_manager is None:
                logger.warning("Socket manager not available")
                return False

            try:
                socket_id = socket_manager.resolve_socket("music")
            except RoomNotBound:
                logger.warning("No music client connected, is the music tab open?")
                return False
            socket_io = DI.socket_io
            if not socket_io:
                logger.warning("Socket IO not available")
                return False

            data = {"command": command, "payload": payload or {}}
            socket_io.emit("music_command", data, room=socket_id)
            logger.debug(f"Sent music command: {command}")
            return True
        except Exception as e:
            logger.error("Failed to send music command: %s", e)
            logger.debug("failed to send music command exception details", exc_info=True)
            return False
