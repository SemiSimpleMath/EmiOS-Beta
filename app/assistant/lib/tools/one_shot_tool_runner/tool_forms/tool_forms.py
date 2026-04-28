from typing import Optional

from pydantic import BaseModel


class one_shot_tool_runner_args(BaseModel):
    task: str
    information: Optional[str]


class one_shot_tool_runner_arguments(BaseModel):
    tool_name: str
    arguments: one_shot_tool_runner_args
