from app.assistant.lib.core_tools.event_link_tool.event_link_tool import EventLinkTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(EventLinkTool)

