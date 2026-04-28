from app.assistant.lib.core_tools.email_tool.email_tool import EmailTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(EmailTool)
