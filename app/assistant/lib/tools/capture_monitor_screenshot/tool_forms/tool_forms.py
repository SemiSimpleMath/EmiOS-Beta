from pydantic import BaseModel, Field


class capture_monitor_screenshot_args(BaseModel):
    monitor_index: int = Field(1, description="1-based monitor index to capture.")


class capture_monitor_screenshot_arguments(BaseModel):
    tool_name: str
    arguments: capture_monitor_screenshot_args
