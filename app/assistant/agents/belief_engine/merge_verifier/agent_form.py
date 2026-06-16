from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Yes/no merge decision for belief_engine::merge_verifier."""

    same: bool = Field(
        description="True if the two phrasings express the SAME belief/concept for this person; otherwise False.",
    )
    reason: str = Field(
        description="Short justification — for logging/inspection, not control flow.",
    )
