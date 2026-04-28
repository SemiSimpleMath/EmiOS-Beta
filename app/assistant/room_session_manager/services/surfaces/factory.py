from __future__ import annotations

from app.assistant.utils.logging_config import get_logger

from .slack_inbound_service import SlackInboundService
from .sms_inbound_service import SmsInboundService
from .telegram_inbound_service import TelegramInboundService
from .ui_inbound_service import UiInboundService

logger = get_logger(__name__)


class RoomSurfaceHandlerFactory:
    def __init__(self):
        self._handlers = {
            "telegram": TelegramInboundService(),
            "ui": UiInboundService(),
            "sms": SmsInboundService(),
            "slack": SlackInboundService(),
        }

    def get(self, surface: str):
        key = str(surface or "").strip().lower()
        handler = self._handlers.get(key)
        if handler is None:
            raise ValueError(f"Unsupported room surface handler: {surface!r}")
        return handler
