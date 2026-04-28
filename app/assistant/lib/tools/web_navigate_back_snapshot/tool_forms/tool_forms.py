from pydantic import BaseModel


class web_navigate_back_snapshot_args(BaseModel):
    pass


class web_navigate_back_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: web_navigate_back_snapshot_args
