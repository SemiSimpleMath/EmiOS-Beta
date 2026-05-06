"""
Tool forms for taxonomy_path_finder tool.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


class taxonomy_path_finder_args(BaseModel):
    """Input schema for taxonomy_path_finder tool."""
    
    top_level_taxonomy: str = Field(
        description="The top-level taxonomy category to search within (e.g., 'entity', 'state', 'event')"
    )
    
    description: str = Field(
        description="Description of what kind of paths to look for (e.g., 'paths related to robots and machines')"
    )
    
    keywords: Optional[List[str]] = Field(
        default=None,
        description="List of keywords to filter taxonomy paths (e.g., ['robot', 'machine', 'automation'])"
    )

class taxonomy_path_finder_arguments(BaseModel):
    """Tool wrapper for taxonomy_move_category."""
    tool_name: str
    arguments: taxonomy_path_finder_args