# app/assistant/agent/writer/agent_args.py

from pydantic import BaseModel, Field
from typing import Optional, Literal

class mathematician_args(BaseModel):
    """
    Arguments for the writer agent.
    """
    mathematician_task: str = Field(
        ...,
        description="What the agent is being asked to do, expressed as a clear instruction in natural language."
    )

    mathematician_context: str = Field(
        ...,
        description="All the relevant facts, information, or background needed to carry out the task."
    )


class mathematician_arguments(BaseModel):
    arguments: mathematician_args
