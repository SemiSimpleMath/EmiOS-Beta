from app.assistant.lib.core_tools.kg_query.kg_query_tool import KGQueryTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(KGQueryTool)
