from typing import List, Optional
from pydantic import BaseModel, Field


class TaggedFact(BaseModel):
    """One fact's section assignment."""
    fact_id: str = Field(
        ...,
        description=(
            "The fact's identifier as provided in the candidate_facts input "
            "(e.g. 'f1', 'f2'). Use the exact id from the input."
        ),
    )
    section: str = Field(
        ...,
        description=(
            "The section name to file this fact under. MUST be one of the "
            "section names listed in the section_list input — or 'reject' "
            "if the fact is a closed state, past one-off event, or other "
            "content that belongs on the wiki rather than the NOW-snapshot card."
        ),
    )
    confidence: str = Field(
        ...,
        description="'high' | 'medium' | 'low' — your confidence in the section assignment.",
    )
    reason: str = Field(
        ...,
        description="<= 20 words. Brief justification, especially for 'reject' decisions.",
    )


class AgentForm(BaseModel):
    """Section-tagger output: classify each candidate fact into a card section."""
    tagged_facts: List[TaggedFact] = Field(
        ...,
        description=(
            "One TaggedFact per candidate. Must include EVERY fact_id from the "
            "input — do not drop any. Use 'reject' for facts that don't belong "
            "on a NOW-snapshot card."
        ),
    )
