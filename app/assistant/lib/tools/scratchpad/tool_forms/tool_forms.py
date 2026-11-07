from pydantic import BaseModel


class scratchpad_args(BaseModel):
    text: str


class scratchpad_arguments(BaseModel):
    tool_name: str
    arguments: scratchpad_args
