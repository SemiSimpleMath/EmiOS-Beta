from typing import Optional

from pydantic import BaseModel


class get_scheduler_events_args(BaseModel):
    start_date: str
    end_date: str


class get_scheduler_events_arguments(BaseModel):
    tool_name: str
    arguments: get_scheduler_events_args
