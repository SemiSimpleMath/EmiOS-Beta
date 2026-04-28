from pydantic import BaseModel, Field
from typing import List


class CriticDecision(BaseModel):
    number: int = Field(..., description="The candidate number from the input list")
    label: str = Field(..., description="The entity label")
    verdict: str = Field(..., description="KEEP or REJECT")
    reason: str = Field(..., description="One-sentence justification")


class AgentForm(BaseModel):
    """Batch critic output: one decision per candidate entity label."""
    decisions: List[CriticDecision] = Field(
        ...,
        description="One entry per candidate, in the same order as the input list",
    )
