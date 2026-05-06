from pydantic import BaseModel


class taxonomy_team_manager_args(BaseModel):
    task: str
    information: str

class taxonomy_team_manager_arguments(BaseModel):
    tool_name: str
    arguments: taxonomy_team_manager_args
