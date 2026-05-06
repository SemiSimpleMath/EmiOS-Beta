"""
Pydantic schemas for taxonomy_move_category tool
"""
from pydantic import BaseModel, Field
from typing import Optional


class taxonomy_move_category_args(BaseModel):
    """Input schema for taxonomy_move_category tool."""
    category_id: int = Field(description="ID of the category to move")
    new_parent_id: Optional[int] = Field(default=None, description="ID of the new parent category (None or empty for root level)")


class taxonomy_move_category_arguments(BaseModel):
    """Tool wrapper for taxonomy_move_category."""
    tool_name: str
    arguments: taxonomy_move_category_args

