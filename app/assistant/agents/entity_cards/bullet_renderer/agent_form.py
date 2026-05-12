from typing import List
from pydantic import BaseModel, Field


class RenderedBullet(BaseModel):
    """One bullet + the input fact_ids it was synthesized from (sidecar)."""

    bullet_text: str = Field(
        ...,
        description=(
            "ONE bullet, ≤ 20 words, present tense, factually anchored to the "
            "subject. Plain prose — no markdown bullet markers, no quotes, no "
            "emojis. For relationships involving the user (Jukka), use third-"
            "person framing that names the user ('Jukka's wife', 'Jukka's "
            "daughter Annika'). Card prose is read by agents — 'my wife' "
            "leaves them guessing whose wife."
        ),
    )
    source_fact_ids: List[str] = Field(
        ...,
        description=(
            "The fact_id values from the input that this bullet was "
            "synthesized from. A bullet that fuses multiple atomic facts "
            "lists all their fact_ids. Every input fact you keep must appear "
            "in exactly one bullet's source_fact_ids."
        ),
    )


class AgentForm(BaseModel):
    """Bullet renderer output: all bullets for the section, with sidecar."""

    bullets: List[RenderedBullet] = Field(
        ...,
        description=(
            "All bullets for this section. Fuse atomic facts that express "
            "the same underlying claim into a single bullet. Drop facts that "
            "are clearly redundant with another bullet — list them once."
        ),
    )
