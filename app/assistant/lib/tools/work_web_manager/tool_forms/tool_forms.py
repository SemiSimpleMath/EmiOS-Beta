from pydantic import BaseModel


class work_web_manager_args(BaseModel):
    task: str
    information: str

class work_web_manager_arguments(BaseModel):
    tool_name: str
    arguments: work_web_manager_args
