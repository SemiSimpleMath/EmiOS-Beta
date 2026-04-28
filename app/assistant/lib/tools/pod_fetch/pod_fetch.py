from app.assistant.lib.core_tools.pod_store.pod_store_tool import PodStoreTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(PodStoreTool)
