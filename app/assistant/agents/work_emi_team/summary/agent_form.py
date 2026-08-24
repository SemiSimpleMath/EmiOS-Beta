"""Output contract for a summary agent — the shape summary_post_node applies.

Mirrors the contract every summary agent shares (agents/<ns>/summary/agent_form.py):
history_id-keyed compressions plus visibility/lifecycle lists. Forms are loaded
per-directory by path, so each summary agent carries its own copy; the DOMAIN
rules live in prompts/system.j2, not here.
"""
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
