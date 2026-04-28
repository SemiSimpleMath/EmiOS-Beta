from pydantic import BaseModel

class create_repeating_scheduler_event_args(BaseModel):
    title: str
    start_date: str
    end_date: str | None
    interval: int  # seconds
    payload_message: str
    task_type: str
    importance: int
    sound: str


class create_repeating_scheduler_event_arguments(BaseModel):
    tool_name: str
    arguments: create_repeating_scheduler_event_args
