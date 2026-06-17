from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Yes/no merge decision for belief_engine::merge_verifier."""

    same: bool = Field(
        description="True if the two phrasings express the SAME belief/concept for this person; otherwise False.",
    )
    reason: str = Field(
        description="Short justification — for logging/inspection, not control flow.",
    )
    canonical_statement: str = Field(
        default="",
        description=(
            "When same=True, the SINGLE reconciled statement that should replace both beliefs. "
            "It must preserve EVERY load-bearing detail from each — if one is a fuller version of "
            "the other, return the fuller one; if each carries a detail the other lacks, combine "
            "them faithfully. Never drop a clause, qualifier, or instruction, and never add a claim "
            "neither states. When same=False, leave empty."
        ),
    )
