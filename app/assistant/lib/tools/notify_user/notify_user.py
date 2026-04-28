import uuid
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

class NotifyUserTool(BaseTool):
    def __init__(self):
        super().__init__('notify_user')

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        logger.debug("notify_user execute called")

        notify_info = tool_message.tool_data.get('arguments', {}).get('notify_info')
        if not notify_info:
            return ToolResult(result_type='error', content="Missing 'notify_info' argument.")

# --------- Notify user message for the user
        notify_tool_result = ToolResult(result_type="ask_user", content=notify_info, data_list=[{}])
        user_msg = ToolMessage(
            tool_name="notify_user",
            receiver=None,
            sender="agent",
            content=notify_info,
            tool_result=notify_tool_result
        )

        user_msg.event_topic = 'notify_user_request'

        DI.event_hub.publish(user_msg)
        logger.info(f"[notify_user] Notification sent to user: {notify_info}")

# --------- Return ToolMessage for the agents
        tool_result = ToolResult(
            result_type="notify_user",
            content=f"User has been successfully notified.  Following was sent to user: {notify_info}. You will not get additional info back from user.",
            data_list=[]
        )

        tool_msg = ToolMessage(
            tool_name="notify_user",
            receiver=None,
            sender="agent",
            content=notify_info,
            tool_result=tool_result
        )
        return tool_result

def get_tool_class():
    return NotifyUserTool
