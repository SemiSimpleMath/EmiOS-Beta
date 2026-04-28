from pydantic import BaseModel


class playwright_manager_args(BaseModel):
    task: str
    information: str


class playwright_manager_arguments(BaseModel):
    tool_name: str
    arguments: playwright_manager_args
