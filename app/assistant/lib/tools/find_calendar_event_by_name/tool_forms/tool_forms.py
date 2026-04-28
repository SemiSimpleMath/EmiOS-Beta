from typing import Optional
from pydantic import BaseModel


class find_calendar_event_by_name_args(BaseModel):
    event_name: str
    start_date: Optional[str] = None  # Optional: limit search to events after this date
    end_date: Optional[str] = None    # Optional: limit search to events before this date


class find_calendar_event_by_name_arguments(BaseModel):
    tool_name: str
    arguments: find_calendar_event_by_name_args

