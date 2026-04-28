from typing import Optional, List, Literal

from pydantic import BaseModel


class update_calendar_event_args(BaseModel):
    event_id: str  # Required for updates
    summary: Optional[str] = None  # Event title
    description: Optional[str] = None  # Event description
    start: Optional[str] = None
    end: Optional[str] = None
    recurrence: Optional[List[str]] = None  # e.g., ["RRULE:FREQ=WEEKLY;COUNT=10"]
    location: Optional[str] = None  # Event location
    email: Optional[str] = None  # Organizer's email address
    scope: Optional[Literal["single", "all"]] = None  # For recurring events: "single" (just this occurrence) or "all" (entire series)
    flexibility: Optional[Literal["fixed", "flexible", "soft_block", "aspirational"]] = None
    blocking: Optional[bool] = None  # True=blocks time, False=doesn't block

class update_calendar_event_arguments(BaseModel):
    tool_name: str
    arguments: update_calendar_event_args