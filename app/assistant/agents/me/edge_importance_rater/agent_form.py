from typing import List

from pydantic import BaseModel, Field


class EdgeImportance(BaseModel):
    id: str = Field(description="Exact edge id from the input batch.")
    reason: str = Field(
        description=(
            "Brief explanation (1-2 sentences) of why this edge gets the "
            "score it does, from the source's perspective. State what the "
            "EDGE SENTENCE says happens, why that's important (or not) to "
            "the source, and which calibration band it lands in. Write the "
            "reason BEFORE settling on the score."
        ),
    )
    score: float = Field(
        description=(
            "Importance of this edge from the source's perspective, 0.0 to 10.0. "
            "Family bonds, marriage, primary parents/children → 9-10. "
            "Close friends, significant teachers/mentors, primary employer → 7-8. "
            "Regular colleagues, recurring collaborators → 5-6. "
            "Acquaintances, one-time meetings, occasional attendance → 3-4. "
            "One-off references, mere mentions, extraction artifacts → 0-2."
        ),
    )


class AgentForm(BaseModel):
    """Output for me::edge_importance_rater.

    Returns one importance score per input edge. The id MUST match the
    input verbatim — no inventing or paraphrasing ids.
    """

    ratings: List[EdgeImportance] = Field(
        default_factory=list,
        description=(
            "One entry per input edge, same length as the input batch. "
            "Always populate — empty array is invalid."
        ),
    )
