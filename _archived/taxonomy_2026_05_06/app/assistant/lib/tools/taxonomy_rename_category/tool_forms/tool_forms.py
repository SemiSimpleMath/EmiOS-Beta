"""
Pydantic schemas for taxonomy_rename_category tool
"""
from pydantic import BaseModel, Field


class taxonomy_rename_category_args(BaseModel):
    """Input schema for taxonomy_rename_category tool."""
    category_id: int = Field(description="ID of the category to rename")
    new_label: str = Field(description="New label for the category")


class taxonomy_rename_category_arguments(BaseModel):
    """Tool wrapper for taxonomy_rename_category."""
    tool_name: str
    arguments: taxonomy_rename_category_args

