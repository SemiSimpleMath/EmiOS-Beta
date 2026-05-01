from pydantic import BaseModel


class kg_dev_manager_args(BaseModel):
    task: str
    information: str


class kg_dev_manager_arguments(BaseModel):
    tool_name: str
    arguments: kg_dev_manager_args
