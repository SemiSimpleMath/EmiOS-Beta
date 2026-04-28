from typing import List, Optional

from pydantic import BaseModel


class get_calendar_events_args(BaseModel):
    start_date: str
    end_date: str
    calendar_names: Optional[List[str]] = None
    single_events: bool = True  # Default True to get actual instances, not recurring rule definitions
    repo_update: Optional[bool] = False  # Whether to update EventRepository


class get_calendar_events_arguments(BaseModel):
    tool_name: str
    arguments: get_calendar_events_args

