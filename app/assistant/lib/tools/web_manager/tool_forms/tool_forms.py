from pydantic import BaseModel


class web_manager_args(BaseModel):
    task: str
    information: str

class web_manager_arguments(BaseModel):
    tool_name: str
    arguments: web_manager_args
