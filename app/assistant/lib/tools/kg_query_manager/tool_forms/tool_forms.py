from pydantic import BaseModel


class kg_query_manager_args(BaseModel):
    task: str
    information: str

class kg_query_manager_arguments(BaseModel):
    tool_name: str
    arguments: kg_query_manager_args

