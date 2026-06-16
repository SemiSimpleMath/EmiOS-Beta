from typing import List

from pydantic import BaseModel, Field


class BeliefTagging(BaseModel):
    id: str = Field(
        description="Exact belief ref (the b<N> id) from the input batch — copy it verbatim; do not invent or reformat.",
    )
    tags: List[str] = Field(
        default_factory=list,
        description=(
            "Every retrieval tag that genuinely applies to this belief, chosen ONLY from the "
            "ALLOWED TAGS list. Multi-label — most beliefs carry 1-4. Never emit a tag outside "
            "the allowed list."
        ),
    )


class AgentForm(BaseModel):
    """Output for belief_engine::belief_tagger — one entry per input belief, ref verbatim."""

    assignments: List[BeliefTagging] = Field(
        default_factory=list,
        description="One entry per input belief, same length as the batch. Always populate.",
    )
