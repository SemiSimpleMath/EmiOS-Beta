from typing import List, Literal

from pydantic import BaseModel, Field


class ArtifactDecision(BaseModel):
    artifact_id: str = Field(description="Canonical artifact id from New Potential Artifacts.")
    decision: Literal[
        "ADMIT", "ADMIT_ARTIFACT",
        "REJECT_DUPLICATE", "REJECT_NO_ACTION", "REJECT_POLICY",
    ] = Field(
        description=(
            "ADMIT = needs a plan (actionable). "
            "ADMIT_ARTIFACT = useful context, no plan needed. "
            "REJECT_* = noise/duplicate/policy."
        )
    )
    reason: str = Field(description="Short, specific reason for the admission decision.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score in [0.0, 1.0].",
    )


class AgentForm(BaseModel):
    triage_summary: str = Field(description="Short summary of admission outcomes for this pass.")
    artifact_decisions: List[ArtifactDecision] = Field(
        default_factory=list,
        description="One admission decision per artifact in New Potential Artifacts.",
    )

