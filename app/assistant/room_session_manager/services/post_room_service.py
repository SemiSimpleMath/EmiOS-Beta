from __future__ import annotations

from typing import Any, Dict, Tuple

from app.assistant.room_session_manager.contracts import OutboundIntent
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PostRoomService:
    @staticmethod
    def extract_reply_text(manager_result: Any) -> Tuple[str, Dict[str, Any]]:
        data = getattr(manager_result, "data", None)
        if isinstance(data, dict):
            # Explicit no-op contract: never emit outbound text.
            if bool(data.get("final_answer_no_op", False)):
                return "", data

            final = data.get("final_answer_answer")
            if isinstance(final, str) and final.strip():
                return final.strip(), data
            # Final-answer schema present but the answer is empty → no reply.
            if "final_answer_answer" in data:
                return "", data

        # Text reaches a user ONLY via final_answer_answer (above) or the manager's
        # own `content` — never by serializing arbitrary `data` values, which would
        # leak internal manager fields into chat/slack. A data dict with neither
        # channel yields no reply.
        content = getattr(manager_result, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip(), data if isinstance(data, dict) else {}
        if isinstance(data, dict) and data:
            logger.debug(
                "PostRoomService: manager result had data but no final_answer/content reply "
                "channel; no reply emitted (keys=%s)", sorted(data.keys()),
            )
        return "", data if isinstance(data, dict) else {}

    def build_outbound_intent(
        self,
        *,
        request_id: str,
        room_id: str,
        room_surface: str,
        room_context_id: str,
        reply_to: Dict[str, Any],
        manager_result: Any,
        send_reply: bool,
    ) -> OutboundIntent:
        reply_text, payload = self.extract_reply_text(manager_result)
        send = bool(send_reply and isinstance(reply_text, str) and reply_text.strip())
        return OutboundIntent(
            request_id=request_id,
            room_id=room_id,
            room_surface=room_surface,
            room_context_id=room_context_id,
            reply_text=reply_text.strip(),
            send=send,
            delivery_mode="auto_send" if send else "draft",
            reply_to=reply_to if isinstance(reply_to, dict) else {},
            payload=payload if isinstance(payload, dict) else {},
            metadata={"send_reply_requested": bool(send_reply)},
        )
