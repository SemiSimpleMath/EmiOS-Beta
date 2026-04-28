from pydantic import BaseModel, Field


class web_navigate_snapshot_args(BaseModel):
    url: str = Field(description="Absolute URL to navigate to.")


class web_navigate_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: web_navigate_snapshot_args
