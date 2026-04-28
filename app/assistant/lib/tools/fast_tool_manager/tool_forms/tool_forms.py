from pydantic import BaseModel


class fast_tool_manager_args(BaseModel):
    task: str
    information: str


class fast_tool_manager_arguments(BaseModel):
    tool_name: str
    arguments: fast_tool_manager_args
