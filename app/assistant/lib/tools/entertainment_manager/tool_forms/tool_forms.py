from pydantic import BaseModel


class entertainment_manager_args(BaseModel):
    task: str
    information: str


class entertainment_manager_arguments(BaseModel):
    tool_name: str
    arguments: entertainment_manager_args
