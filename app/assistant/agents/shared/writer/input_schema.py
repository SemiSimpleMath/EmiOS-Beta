# app/assistant/agent/writer/agent_args.py

from pydantic import BaseModel, Field
from typing import Optional, Literal

class writer_args(BaseModel):
    """
    Arguments for the writer agent.
    """
    writer_task: str = Field(
        ...,
        description="What the agent is being asked to do, expressed as a clear instruction in natural language."
    )

    writer_context: str = Field(
        ...,
        description="All the relevant facts, information, or background needed to carry out the task."
    )

    purpose: Literal[
        "email", "poem", "story", "memo", "report", "summary", "research", "other"
    ] = Field(..., description="The type of writing to generate.")

    audience: Optional[str] = Field(
        None,
        description="The intended audience or recipient (e.g., 'my boss', 'a friend', 'a hiring manager')."
    )

    tone: Optional[str] = Field(
        None,
        description="Desired tone or voice (e.g., 'friendly', 'professional', 'apologetic', 'playful')."
    )

    length: Optional[Literal["short", "medium", "long"]] = Field(
        None,
        description="Desired length of the output."
    )

    format_constraints: Optional[str] = Field(
        None,
        description="Any formatting constraints or special instructions (e.g., bullet points, title only, single paragraph)."
    )


class writer_arguments(BaseModel):
    arguments: writer_args
