from typing import Optional

from pydantic import BaseModel, Field


class web_click_ref_snapshot_args(BaseModel):
    ref: str = Field(description="Target ref id from browser_snapshot (for example e10).")
    element: Optional[str] = Field(
        default=None,
        description="Optional element label/context (e.g. the visible text or aria-label) to disambiguate the click target.",
    )


class web_click_ref_snapshot_arguments(BaseModel):
    tool_name: str
    arguments: web_click_ref_snapshot_args
