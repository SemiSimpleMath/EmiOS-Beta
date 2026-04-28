from pydantic import BaseModel


class event_manager_args(BaseModel):
    task: str
    information: str


class event_manager_arguments(BaseModel):
    tool_name: str
    arguments: event_manager_args