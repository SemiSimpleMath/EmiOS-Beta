from typing import List

from pydantic import BaseModel, Field


class NodeImportance(BaseModel):
    id: str = Field(description="Exact node id from the input batch.")
    score: float = Field(
        description=(
            "Importance to the user (Jukka), 0.0 to 10.0. "
            "Family / partner / children → 9-10. "
            "Close friends / colleagues / pets → 6-8. "
            "Acquaintances, places visited, ongoing hobbies → 3-5. "
            "Mentioned-once items, products, generic concepts, single-use names → 0-2."
        ),
    )


class AgentForm(BaseModel):
    """Output for me::importance_rater.

    Returns one importance score per input node. The id MUST match the
    input verbatim — no inventing or paraphrasing ids.
    """

    ratings: List[NodeImportance] = Field(
        default_factory=list,
        description=(
            "One entry per input node, same length as the input batch. "
            "Always populate — empty array is invalid."
        ),
    )
