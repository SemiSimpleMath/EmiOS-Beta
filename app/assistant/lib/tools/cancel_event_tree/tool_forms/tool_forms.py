from pydantic import BaseModel, Field


class cancel_event_tree_args(BaseModel):
    """Arguments for cancelling an event tree."""
    
    source: str = Field(
        ...,
        description="Event reference in format 'source_system:source_id'. "
                    "This event and ALL its children will be marked as cancelled."
    )


class cancel_event_tree_arguments(BaseModel):
    """Tool call wrapper."""
    tool_name: str = Field(default="cancel_event_tree")
    arguments: cancel_event_tree_args

