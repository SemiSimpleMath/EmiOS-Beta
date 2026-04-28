from pydantic import BaseModel


class personal_admin_manager_args(BaseModel):
    task: str
    information: str


class personal_admin_manager_arguments(BaseModel):
    tool_name: str
    arguments: personal_admin_manager_args
