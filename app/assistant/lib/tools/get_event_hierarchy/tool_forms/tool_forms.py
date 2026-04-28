from pydantic import BaseModel, Field


class get_event_hierarchy_args(BaseModel):
    """Arguments for getting event hierarchy."""
    
    source: str = Field(
        ...,
        description="Event reference in format 'source_system:source_id'. "
                    "Examples: 'google_calendar:abc123', 'scheduler:reminder_001'"
    )


class get_event_hierarchy_arguments(BaseModel):
    """Tool call wrapper."""
    tool_name: str = Field(default="get_event_hierarchy")
    arguments: get_event_hierarchy_args

