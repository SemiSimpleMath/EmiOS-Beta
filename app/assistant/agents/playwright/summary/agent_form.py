from typing import List

from pydantic import BaseModel, Field


class SummaryPair(BaseModel):
    history_id: str = Field(description="History id as numeric string, e.g. '19'.")
    summary: str = Field(description="Summary text for that history id.")


class AgentForm(BaseModel):
    summary_pairs: List[SummaryPair] = Field(
        default_factory=list,
        description="List of (history_id, summary) items.",
    )
    hide_ids: List[int] = Field(default_factory=list)
    unhide_ids: List[int] = Field(default_factory=list)
    pin_ids: List[int] = Field(default_factory=list)
    delete_ids: List[int] = Field(default_factory=list)
    reasoning: str
