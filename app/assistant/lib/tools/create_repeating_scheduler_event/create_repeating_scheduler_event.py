from app.assistant.lib.core_tools.scheduler_tool.scheduler_tool import SchedulerTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(SchedulerTool)