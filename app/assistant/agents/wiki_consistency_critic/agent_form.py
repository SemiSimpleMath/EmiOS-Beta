from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ConsistencyFinding(BaseModel):
    issue_type: Literal[
        "contradiction",
        "tense_mismatch",
        "duplicate_entity",
        "impossible_sequence",
        "other",
    ]
    summary: str
    quoted_text: str
    confidence: float = Field(ge=0.0, le=1.0)
    # When the conflict is with the ground-truth block, these two fields name
    # WHICH authoritative source is being disputed, so a human reviewer can
    # tell whether the dispute is with the entity card, the user profile, or
    # an internal inconsistency. Both are required when and only when the
    # conflict is with the ground-truth block — leave empty for purely
    # internal issues (tense_mismatch, duplicate_entity, etc.).
    source_kind: Optional[
        Literal[
            "entity_card_summary",
            "entity_card_key_fact",
            "user_profile",
            "internal",
        ]
    ] = Field(default=None)
    source_statement: Optional[str] = Field(
        default=None,
        description=(
            "The verbatim line from the ground-truth block that the prose "
            "contradicts. Empty for internal-only issues."
        ),
    )


class AgentForm(BaseModel):
    """Output of the wiki_consistency_critic agent.

    `findings` is a list of concrete issues spotted while reading the page;
    each finding quotes the phrase(s) that triggered it. Empty list means
    the page read as internally coherent.
    """

    findings: List[ConsistencyFinding]
    reason: str
