from pydantic import BaseModel


class work_emi_team_manager_args(BaseModel):
    task: str
    information: str


class work_emi_team_manager_arguments(BaseModel):
    tool_name: str
    arguments: work_emi_team_manager_args
