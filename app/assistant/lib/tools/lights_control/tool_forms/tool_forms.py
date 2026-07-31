from typing import Literal

from pydantic import BaseModel


class lights_control_args(BaseModel):
    command: Literal[
        "list_lights",
        "set_light_power",
    ]
    light_id: str | None = None
    room: str | None = None
    state: str | None = None


class lights_control_arguments(BaseModel):
    tool_name: str
    arguments: lights_control_args
