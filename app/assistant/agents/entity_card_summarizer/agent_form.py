"""Structured output for the entity card summarizer.

The agent produces four fields. Everything it claims must be grounded in
the tagged bullets it was given — the writer is a summarizer, not an
inferer.

``relevance_reason`` is optional: the short "why does this entity matter
to the user" one-liner that drives prompt-injection priority.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ContactInfo(BaseModel):
    type: Literal["email", "phone", "address", "link", "handle", "other"]
    value: str = Field(..., description="The actual contact value as it appears in the bullets.")
    label: str = Field("", description="Optional short context like 'work', 'personal', 'home'.")


class AgentForm(BaseModel):
    summary: str = Field(
        ...,
        description=(
            "One or two natural sentences capturing who/what this entity is "
            "and their most defining relationship to the primary user. "
            "Grounded in the tagged bullets only. Keep it short enough to "
            "paste into a prompt header."
        ),
    )
    key_facts: List[str] = Field(
        default_factory=list,
        description=(
            "Short bullet-style facts about the entity worth surfacing "
            "during prompt injection. 3-10 items typical. Each should be "
            "one phrase or sentence, specific, grounded in the bullets. "
            "Order by importance."
        ),
    )
    contact_info: List[ContactInfo] = Field(
        default_factory=list,
        description=(
            "Contact entries extracted from the bullets. Include every "
            "distinct email / phone / address / handle / link that appears. "
            "Preserve values verbatim from the source — do not normalise or "
            "rewrite."
        ),
    )
    relevance_reason: Optional[str] = Field(
        None,
        description=(
            "Optional one-liner describing why this entity matters in the "
            "user's life (e.g. 'primary user', 'spouse of primary user', "
            "'close friend from Finland', 'brand the user follows'). Used "
            "for card-priority heuristics."
        ),
    )
