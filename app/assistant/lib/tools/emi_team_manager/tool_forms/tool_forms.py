from pydantic import BaseModel


class emi_team_manager_args(BaseModel):
    task: str
    information: str

class emi_team_manager_arguments(BaseModel):
    tool_name: str
    arguments: emi_team_manager_args

