from pydantic import BaseModel


class nest_home_control_args(BaseModel):
    # All optional. Pass any combination of state fields to apply them; pass
    # nothing to read current state.
    device_id: str | None = None
    mode: str | None = None
    target_temperature: float | None = None
    fan_mode: str | None = None
    eco_enabled: bool | None = None
    # Explicit get_status flag for callers that prefer being verbose.
    get_status: bool | None = None


class nest_home_control_arguments(BaseModel):
    tool_name: str
    arguments: nest_home_control_args
