"""
Tool forms for taxonomy_create_category
"""

from pydantic import BaseModel, Field


class taxonomy_create_category_args(BaseModel):
    """Arguments for creating a new taxonomy category."""
    parent_id: int = Field(..., description="ID of the parent category under which to create the new category")
    new_label: str = Field(..., description="Label for the new category (will be normalized to lowercase with underscores)")
    description: str = Field(default="", description="Description for the new category (optional)")


class taxonomy_create_category_arguments(BaseModel):
    """Tool wrapper for taxonomy_merge_categories."""
    tool_name: str
    arguments: taxonomy_create_category_args


