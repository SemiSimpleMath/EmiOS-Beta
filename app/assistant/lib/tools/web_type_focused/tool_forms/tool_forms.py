from pydantic import BaseModel, Field


# IMPORTANT:
# ToolRegistry expects these exact class names:
# - <tool_name>_args
# - <tool_name>_arguments (outer wrapper with tool_name + arguments)
#
# If these are missing, the tool will fail to register and will not appear in allowed tools.


class web_type_focused_args(BaseModel):
    text: str = Field(..., description="Text to type into the currently-focused element.")
    submit: bool = Field(default=True, description="If true, press Enter after typing.")
    clear_first: bool = Field(default=True, description="If true, clear input/textarea before typing (best-effort).")
    slowly: bool = Field(default=False, description="If true, type with a small delay between characters.")
    capture_snapshot: bool = Field(
        default=True,
        description="If true, capture browser_snapshot after action and include it in tool result data.",
    )


class web_type_focused_arguments(BaseModel):
    tool_name: str
    arguments: web_type_focused_args

