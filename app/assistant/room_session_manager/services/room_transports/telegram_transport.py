from __future__ import annotations


class TelegramRoomTransport:
    @staticmethod
    def send_reply(*, chat_id: str, body: str) -> str:
        from app.services.telegram_bot import TelegramBotService

        result = TelegramBotService().send_message(chat_id=chat_id, text=body)
        msg = result.get("result") if isinstance(result, dict) else None
        if isinstance(msg, dict):
            message_id = msg.get("message_id")
            if message_id is not None:
                return str(message_id)
        return ""
