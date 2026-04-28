from app.assistant.lib.core_tools.todo_tool.todo_tool import ToDoTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(ToDoTool)