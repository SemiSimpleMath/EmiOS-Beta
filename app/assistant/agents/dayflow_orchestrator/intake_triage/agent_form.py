from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ArtifactDecision(BaseModel):
    artifact_id: str = Field(description="Canonical artifact id from New Potential Artifacts.")
    decision: Literal[
        "ADMIT",
        "REJECT_DUPLICATE", "REJECT_NO_ACTION", "REJECT_POLICY",
    ] = Field(
        description=(
            "ADMIT = useful, keep it — including when only PART of it is already covered. "
            "REJECT_DUPLICATE = every part of it is already covered. "
            "REJECT_NO_ACTION / REJECT_POLICY = noise or policy violation."
        )
    )
    reason: str = Field(description="Short, specific reason for the decision.")
    uncovered: Optional[str] = Field(
        default=None,
        description=(
            "REQUIRED when you ADMIT something that is PARTLY covered already: name the part "
            "that is NOT yet covered, and say what is already handled so it is not done twice. "
            "e.g. 'already covered: the appointment date. NOT covered: the sender asks us to "
            "tell the user about it.' Leave empty when the artifact is wholly new."
        ),
    )


class AgentForm(BaseModel):
    triage_summary: str = Field(description="Short summary of admission outcomes for this pass.")
    artifact_decisions: List[ArtifactDecision] = Field(
        default_factory=list,
        description="One decision per artifact in New Potential Artifacts.",
    )
