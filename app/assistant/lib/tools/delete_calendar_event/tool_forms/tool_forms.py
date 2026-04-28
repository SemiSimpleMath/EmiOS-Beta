from typing import Optional
from pydantic import BaseModel, Field


class delete_calendar_event_args(BaseModel):
    event_id: str = Field(
        ...,
        description=(
            "The Google Calendar event ID to delete. For a single occurrence of a "
            "recurring event, use the instance id (e.g. 'abc123_20260411T030000Z'). "
            "This tool deletes only one event or one occurrence — never the whole series."
        ),
    )
    cascade: Optional[bool] = Field(
        default=False,
        description="If True, also delete linked children (reminders, sub-events) in the local repository."
    )


class delete_calendar_event_arguments(BaseModel):
    tool_name: str
    arguments: delete_calendar_event_args
