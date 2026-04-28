from __future__ import annotations

from datetime import datetime, timezone
import uuid

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, UserMessage, UserMessageData
from app.assistant.ServiceLocator.service_locator import DI

logger = get_logger(__name__)


class SetUiMuteTool(BaseTool):
    def __init__(self):
        super().__init__("set_ui_mute")

    @staticmethod
    def _normalize_action(raw_action: object) -> str:
        action = str(raw_action or "").strip().lower()
        if action in {"mute", "unmute", "toggle"}:
            return action
        raise ValueError("Invalid action. Expected one of: mute, unmute, toggle.")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        arguments = tool_message.tool_data.get("arguments", {}) if isinstance(tool_message.tool_data, dict) else {}
        action_raw = arguments.get("action")

        try:
            action = self._normalize_action(action_raw)
        except Exception as e:
            logger.error("[set_ui_mute] Invalid action: %r", action_raw)
            logger.debug("[set_ui_mute] action validation error details", exc_info=True)
            return ToolResult(result_type="error", content=f"set_ui_mute failed: {e}")

        request_context = tool_message.tool_data.get("request_context") if isinstance(tool_message.tool_data, dict) else None
        if not isinstance(request_context, dict):
            request_context = {}
        reply_to = request_context.get("reply_to")

        widget = {
            "data_type": "audio_control",
            "action": action,
            "source": "set_ui_mute",
        }

        request_id = str(tool_message.request_id or "").strip() or None
        text = "Muted UI audio." if action == "mute" else "Unmuted UI audio." if action == "unmute" else "Toggled UI audio mute."
        msg = UserMessage(
            data_type="user_msg",
            sender=self.name,
            receiver=None,
            timestamp=datetime.now(timezone.utc),
            id=str(uuid.uuid4()),
            request_id=request_id,
            role="assistant",
            user_message_data=UserMessageData(
                feed=text,
                chat=text,
                widget_data=[widget],
                tts=False,
            ),
            metadata={"reply_to": reply_to} if isinstance(reply_to, dict) else None,
        )
        msg.event_topic = "socket_emit"
        DI.event_hub.publish(msg)

        return ToolResult(
            result_type="tool_success",
            content=text,
            data={"action": action, "widget_data": [widget]},
            data_list=[{"action": action}],
        )


def get_tool_class():
    return SetUiMuteTool
