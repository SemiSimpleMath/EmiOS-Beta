from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """
    Structured output for `web::critic`.

    Synchronous guardrail — does NOT execute tools. The flow controller
    routes to this agent through critic_pre_node and back through
    critic_post_node; nothing here needs to participate in the planner's
    action contract. Form has only what downstream consumers actually read.
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

