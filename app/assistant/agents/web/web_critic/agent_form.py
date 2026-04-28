from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """
    Structured output for `shared::web_critic`.

    This agent is a synchronous guardrail. It must NOT trigger tools directly.
    It returns `action="done"` so control stays in the manager flow.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["done"] = Field(..., description="Always 'done'. This agent does not execute tools.")

    must_revise_plan: bool = Field(
        ...,
        description="True if the planner should STOP and revise before executing the planned tool call.",
    )
    critic_diagnosis_tags: List[str] = Field(
        default_factory=list,
        description="Short tags like: modal_blocking, new_tab, looping, wrong_target, typing_failed, stuck, needs_snapshot, needs_tabs.",
    )
    critic_diagnosis: str = Field(
        ...,
        description="Concise diagnosis of what is going wrong (1-5 sentences).",
    )
    critic_actionable_change: str = Field(
        ...,
        description="One concrete instruction for what the planner should do next.",
    )
    critic_confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="0.0 to 1.0 confidence.",
    )

