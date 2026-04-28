from typing import List

from pydantic import BaseModel, Field


class FilterDecision(BaseModel):
    number: int = Field(..., description="The candidate number from the input list")
    label: str = Field(..., description="The node label")
    verdict: str = Field(..., description="KEEP or SKIP")
    reason: str = Field(..., description="One-sentence justification")


class AgentForm(BaseModel):
    """Batch filter output: one decision per candidate node."""

    decisions: List[FilterDecision] = Field(
        ...,
        description="One entry per candidate, in the same order as the input list",
    )
