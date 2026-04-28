from pydantic import BaseModel


class kg_team_manager_args(BaseModel):
    task: str
    information: str

class kg_team_manager_arguments(BaseModel):
    tool_name: str
    arguments: kg_team_manager_args

