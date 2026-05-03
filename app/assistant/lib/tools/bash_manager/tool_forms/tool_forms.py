from pydantic import BaseModel


class bash_manager_args(BaseModel):
    task: str
    information: str


class bash_manager_arguments(BaseModel):
    tool_name: str
    arguments: bash_manager_args
