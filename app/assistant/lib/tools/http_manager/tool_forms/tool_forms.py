from pydantic import BaseModel


class http_manager_args(BaseModel):
    task: str
    information: str


class http_manager_arguments(BaseModel):
    tool_name: str
    arguments: http_manager_args
