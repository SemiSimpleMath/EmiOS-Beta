"""
Taxonomy Create Category Tool

Creates a new category in the taxonomy under a specified parent.
"""

from app.assistant.lib.core_tools.taxonomy_tool.taxonomy_tool import TaxonomyTool
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader

get_tool_class = create_tool_loader(TaxonomyTool)

