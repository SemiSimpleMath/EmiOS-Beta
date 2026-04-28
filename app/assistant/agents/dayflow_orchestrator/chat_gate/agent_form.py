from typing import Literal

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="One sentence explaining why you chose this action (or no-op) this turn. Always populate this.",
    )
    no_op_tf: bool = Field(
        default=False,
        description=(
            "Set True when no output is needed this turn. "
            "Mutually exclusive with handoff_tf=True. "
            "When True, leave switchboard_task and switchboard_information empty."
        ),
    )
    handoff_tf: bool = Field(
        default=False,
        description=(
            "Set True when switchboard is needed for multi-step/tool work. "
            "Mutually exclusive with no_op_tf=True. "
            "When True, populate switchboard_task and switchboard_information."
        ),
    )
    switchboard_task: str = Field(
        default="",
        description="Task description for switchboard. Required when handoff_tf=True.",
    )
    switchboard_information: str = Field(
        default="",
        description="Supporting context for switchboard. Used when handoff_tf=True.",
    )
