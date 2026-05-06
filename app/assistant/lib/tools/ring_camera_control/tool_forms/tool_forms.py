from pydantic import BaseModel


class ring_camera_control_args(BaseModel):
    command: str
    camera_id: str | None = None
    lookback_minutes: int | None = None
    enabled: bool | None = None


class ring_camera_control_arguments(BaseModel):
    tool_name: str
    arguments: ring_camera_control_args
