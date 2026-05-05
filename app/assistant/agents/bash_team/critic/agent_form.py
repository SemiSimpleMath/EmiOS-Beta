from typing import List

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """
    Structured output for `bash_team::critic`.

    Synchronous guardrail — does NOT execute tools. critic_pre_node and
    critic_post_node handle routing; no participation in the planner's
    action contract is needed.
    """

    model_config = ConfigDict(extra="forbid")

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
