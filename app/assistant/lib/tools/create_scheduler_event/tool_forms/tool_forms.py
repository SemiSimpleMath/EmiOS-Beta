from pydantic import BaseModel

class create_scheduler_event_args(BaseModel):
    title: str
    start_date: str
    task_type: str
    payload_message: str
    importance: int
    sound: str


class create_scheduler_event_arguments(BaseModel):
    tool_name: str
    arguments: create_scheduler_event_args
