from typing import Optional

from pydantic import BaseModel, Field


class set_screen_capture_enabled_args(BaseModel):
    enabled: bool = Field(..., description="True to enable screen capture, False to disable it.")
    actor: Optional[str] = Field("user", description="Who is toggling this (e.g. 'user', 'system').")
    reason: Optional[str] = Field(None, description="Optional reason when disabling.")


class set_screen_capture_enabled_arguments(BaseModel):
    tool_name: str = "set_screen_capture_enabled"
    arguments: set_screen_capture_enabled_args


# Pydantic v2 + postponed annotations: ensure refs are resolved.
set_screen_capture_enabled_arguments.model_rebuild()

