from pydantic import BaseModel


class sandbox_manager_args(BaseModel):
    task: str
    information: str


class sandbox_manager_arguments(BaseModel):
    tool_name: str
    arguments: sandbox_manager_args
