from pydantic import BaseModel, Field


class web_click_xy_snapshot_args(BaseModel):
    x: float = Field(description="X coordinate in viewport pixels.")
    y: float = Field(description="Y coordinate in viewport pixels.")


class web_click_xy_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: web_click_xy_snapshot_args
