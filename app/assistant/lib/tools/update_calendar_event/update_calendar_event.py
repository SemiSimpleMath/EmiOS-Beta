from app.assistant.lib.core_tools.calendar_tool.calendar_tool import CalendarTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(CalendarTool)