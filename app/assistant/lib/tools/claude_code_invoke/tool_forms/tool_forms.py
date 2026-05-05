from typing import Optional

from pydantic import BaseModel, Field


class claude_code_invoke_args(BaseModel):
    task: str = Field(
        ...,
        description="The user's coding request — verbatim or lightly polished.",
    )
    information: Optional[str] = Field(
        default="",
        description="Optional supporting context from earlier turns.",
    )


class claude_code_invoke_arguments(BaseModel):
    tool_name: str
    arguments: claude_code_invoke_args


claude_code_invoke_arguments.model_rebuild()
