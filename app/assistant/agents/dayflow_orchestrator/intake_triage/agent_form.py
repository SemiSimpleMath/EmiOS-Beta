from typing import List, Literal

from pydantic import BaseModel, Field


class ArtifactDecision(BaseModel):
    artifact_id: str = Field(description="Canonical artifact id from New Potential Artifacts.")
    decision: Literal[
        "ADMIT",
        "REJECT_DUPLICATE", "REJECT_NO_ACTION", "REJECT_POLICY",
    ] = Field(
        description=(
            "ADMIT = useful, keep it. "
            "REJECT_* = noise/duplicate/policy violation."
        )
    )
    reason: str = Field(description="Short, specific reason for the decision.")


class AgentForm(BaseModel):
    triage_summary: str = Field(description="Short summary of admission outcomes for this pass.")
    artifact_decisions: List[ArtifactDecision] = Field(
        default_factory=list,
        description="One decision per artifact in New Potential Artifacts.",
    )
