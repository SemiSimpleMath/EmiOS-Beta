"""
Pydantic schemas for taxonomy_update_description tool
"""
from pydantic import BaseModel, Field


class taxonomy_update_description_args(BaseModel):
    """Input schema for taxonomy_update_description tool."""
    category_id: int = Field(description="ID of the category to update")
    new_description: str = Field(description="New description for the category (empty string to clear)")


class taxonomy_update_description_arguments(BaseModel):
    """Tool wrapper for taxonomy_update_description."""
    tool_name: str
    arguments: taxonomy_update_description_args

