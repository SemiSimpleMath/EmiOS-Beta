from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    critic_diagnosis: str = Field(
        ...,
        description="Short, concrete diagnosis (1-4 sentences).",
    )
    critic_actionable_change: str = Field(
        ...,
        description=(
            "A single non-negotiable instruction for the planner's NEXT action. "
            "Must be one concrete step that can be executed immediately."
        ),
    )
    must_revise_plan: bool = Field(
        default=False,
        description="If true, cancel the pending tool call and force planner re-plan.",
    )
    critic_confidence: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Confidence in diagnosis/actionable change (0-1).",
    )

