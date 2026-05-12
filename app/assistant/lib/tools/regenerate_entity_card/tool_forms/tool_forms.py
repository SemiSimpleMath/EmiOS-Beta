from typing import Optional

from pydantic import BaseModel, Field


class regenerate_entity_card_args(BaseModel):
    entity_label: str = Field(
        description="Exact label of the entity (e.g. 'Annika', 'Jukka'). Must match an existing KG node.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional one-line context for the audit log explaining why the card is being regenerated.",
    )


class regenerate_entity_card_arguments(BaseModel):
    arguments: regenerate_entity_card_args
