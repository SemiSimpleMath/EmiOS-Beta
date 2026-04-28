from typing import List, Optional
from pydantic import BaseModel, Field


class link_children_to_parent_args(BaseModel):
    """Arguments for linking child events to a parent event."""
    
    parent: str = Field(
        ...,
        description="Parent event reference in format 'source_system:source_id'. "
                    "Examples: 'google_calendar:abc123', 'scheduler:reminder_001'"
    )
    
    children: List[str] = Field(
        ...,
        description="List of child event references, each in format 'source_system:source_id'. "
                    "Examples: ['scheduler:reminder_001', 'scheduler:reminder_002']"
    )
    
    parent_title: Optional[str] = Field(
        None,
        description="Title for the parent event (used if parent node needs to be created)"
    )


class link_children_to_parent_arguments(BaseModel):
    """Tool call wrapper."""
    tool_name: str = Field(default="link_children_to_parent")
    arguments: link_children_to_parent_args

