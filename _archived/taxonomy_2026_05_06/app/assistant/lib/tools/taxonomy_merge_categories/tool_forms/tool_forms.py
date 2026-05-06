"""
Pydantic schemas for taxonomy_merge_categories tool
"""
from pydantic import BaseModel, Field


class taxonomy_merge_categories_args(BaseModel):
    """Input schema for taxonomy_merge_categories tool."""
    source_id: int = Field(description="ID of the category to merge (will be deleted)")
    destination_id: int = Field(description="ID of the category to merge into (will be kept)")


class taxonomy_merge_categories_arguments(BaseModel):
    """Tool wrapper for taxonomy_merge_categories."""
    tool_name: str
    arguments: taxonomy_merge_categories_args

