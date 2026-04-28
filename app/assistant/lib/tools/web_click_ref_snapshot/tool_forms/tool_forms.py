from pydantic import BaseModel, Field


class web_click_ref_snapshot_args(BaseModel):
    ref: str = Field(description="Target ref id from browser_snapshot (for example e10).")


class web_click_ref_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: web_click_ref_snapshot_args
