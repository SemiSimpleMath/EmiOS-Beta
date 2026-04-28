from typing import Optional, List, Dict, Literal

from pydantic import BaseModel


class create_calendar_event_args(BaseModel):
    event_name: str
    start: str
    end: str
    time_zone: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    link: Optional[str] = None
    participants: Optional[List[Dict[str, str]]] = None
    flexibility: Optional[Literal["fixed", "flexible", "soft_block", "aspirational"]] = "fixed"
    blocking: Optional[bool] = True  # True=opaque/busy, False=transparent/free

class create_calendar_event_arguments(BaseModel):
    tool_name: str
    arguments: create_calendar_event_args
