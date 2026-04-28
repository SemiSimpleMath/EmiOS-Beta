from pydantic import BaseModel


class set_ui_mute_args(BaseModel):
    action: str


class set_ui_mute_arguments(BaseModel):
    tool_name: str
    arguments: set_ui_mute_args
