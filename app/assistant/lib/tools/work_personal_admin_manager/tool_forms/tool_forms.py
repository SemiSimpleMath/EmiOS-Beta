from pydantic import BaseModel


class work_personal_admin_manager_args(BaseModel):
    task: str
    information: str


class work_personal_admin_manager_arguments(BaseModel):
    tool_name: str
    arguments: work_personal_admin_manager_args
