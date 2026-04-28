from pydantic import BaseModel


class task_compile_manager_args(BaseModel):
    task: str
    information: str


class task_compile_manager_arguments(BaseModel):
    tool_name: str
    arguments: task_compile_manager_args
