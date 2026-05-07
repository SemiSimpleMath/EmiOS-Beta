from typing import Optional

from pydantic import BaseModel


class regenerate_entity_card_args(BaseModel):
    entity_label: str
    reason: Optional[str] = None


class regenerate_entity_card_arguments(BaseModel):
    tool_name: str
    arguments: regenerate_entity_card_args
