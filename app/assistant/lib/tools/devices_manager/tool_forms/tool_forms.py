from pydantic import BaseModel


class devices_manager_args(BaseModel):
    task: str
    information: str


class devices_manager_arguments(BaseModel):
    tool_name: str
    arguments: devices_manager_args
