from app.assistant.lib.core_tools.kg_edit.kg_edit import KGEdit
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(KGEdit)