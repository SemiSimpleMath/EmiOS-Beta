from typing import Optional
from pydantic import BaseModel, Field


class delete_scheduler_event_args(BaseModel):
    event_id: str = Field(..., description="The scheduler event ID to delete")
    cascade: Optional[bool] = Field(
        default=False,
        description="If True, also delete all linked children (reminders, sub-events) from the event hierarchy"
    )


class delete_scheduler_event_arguments(BaseModel):
    tool_name: str
    arguments: delete_scheduler_event_args
