from typing import List
from pydantic import BaseModel, Field


class DistilledBullet(BaseModel):
    """One final bullet + the source bullets it was distilled from (sidecar)."""

    bullet_text: str = Field(
        ...,
        description=(
            "ONE final bullet, ≤ 20 words, present tense. Rewrite if a fusion "
            "is cleaner than the original; otherwise reuse the input verbatim. "
            "Same third-person framing rules as upstream renderers — name the "
            "user when describing a relationship ('Jukka's wife')."
        ),
    )
    source_bullet_ids: List[str] = Field(
        ...,
        description=(
            "The bullet_id values from the input that this final bullet "
            "represents (selected verbatim and/or fused). Every kept input "
            "bullet appears in exactly one output bullet's source_bullet_ids."
        ),
    )


class AgentForm(BaseModel):
    """Section distiller output: the final, capped, curated bullet set."""

    bullets: List[DistilledBullet] = Field(
        ...,
        description=(
            "The final bullets for this section, capped at max_bullets. Pick "
            "the most identity-defining facts; drop conversational specifics, "
            "minor opinions, and one-off observations. The card is a curated "
            "sketch, not a KG dump."
        ),
    )
