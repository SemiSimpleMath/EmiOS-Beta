from pydantic import BaseModel, Field


class web_fill_ref_args(BaseModel):
    ref: str = Field(..., description="Snapshot ref of the input element to click and fill.")
    text: str = Field(..., description="Text to type after clicking to focus.")
    submit: bool = Field(default=True, description="If true, press Enter after typing.")
    clear_first: bool = Field(default=True, description="If true, clear the field before typing (best-effort).")
    slowly: bool = Field(default=False, description="If true, type with a small delay between characters.")
    capture_snapshot: bool = Field(
        default=True,
        description="If true, capture browser_snapshot after action and include it in tool result data.",
    )


class web_fill_ref_arguments(BaseModel):
    tool_name: str
    arguments: web_fill_ref_args
