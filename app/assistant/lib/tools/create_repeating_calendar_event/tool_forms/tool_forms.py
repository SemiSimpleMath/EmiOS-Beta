from pydantic import BaseModel

from typing import Union, Dict, List, Optional, Literal


class Participant(BaseModel):
    name: Optional[str]  # Optional so models can leave it null
    email: Optional[str]     # Enforces valid email format


class create_repeating_calendar_event_args(BaseModel):
    event_name: str
    start: str
    end: str
    time_zone: Union[str, None]
    recurrence_rule: str
    calendar_name: Optional[str] = "primary"  # Target calendar (e.g., "Birthdays", "Work")
    all_day: Optional[bool] = False  # True for all-day events, False for timed events
    description: Union[str, None]
    location: Union[str, None]
    link: Union[str, None]
    participants: Optional[List[Participant]]
    flexibility: Optional[Literal["fixed", "flexible", "soft_block", "aspirational"]] = "fixed"
    blocking: Optional[bool] = True  # True=opaque/busy, False=transparent/free


class create_repeating_calendar_event_arguments(BaseModel):
    tool_name: str
    arguments: create_repeating_calendar_event_args
