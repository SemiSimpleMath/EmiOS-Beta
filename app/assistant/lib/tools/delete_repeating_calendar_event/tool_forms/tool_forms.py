from typing import Optional
from pydantic import BaseModel, Field


class delete_repeating_calendar_event_args(BaseModel):
    event_id: str = Field(
        ...,
        description=(
            "The Google Calendar event ID of the recurring series. May be either "
            "the base series id or any single instance id — either resolves to the "
            "same series and every occurrence is deleted. Non-recurring events are "
            "rejected; use delete_calendar_event for those."
        ),
    )
    cascade: Optional[bool] = Field(
        default=False,
        description="If True, also delete linked children (reminders, sub-events) in the local repository.",
    )


class delete_repeating_calendar_event_arguments(BaseModel):
    tool_name: str
    arguments: delete_repeating_calendar_event_args
