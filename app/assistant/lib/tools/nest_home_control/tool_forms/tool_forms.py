from pydantic import BaseModel


class nest_home_control_args(BaseModel):
    action: str
    device_id: str | None = None
    mode: str | None = None
    target_temperature: float | None = None
    enabled: bool | None = None
    fan_mode: str | None = None


class nest_home_control_arguments(BaseModel):
    tool_name: str
    arguments: nest_home_control_args
