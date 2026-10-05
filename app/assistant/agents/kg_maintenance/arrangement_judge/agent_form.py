"""Output schema for kg_maintenance::arrangement_judge: is an open State still current, or did a later statement
that shares a participant with it replace it?"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(description="What the statements say and how they relate, citing their dates.")
    verdict: str = Field(
        description="'supersedes' when a later statement replaced the open State's arrangement; 'both_true' when "
                    "both hold at once; 'same_statement' when a later one says the same again; 'unrelated' when "
                    "they concern different arrangements; 'unsure' when the evidence does not settle it.")
    superseded_by: Optional[str] = Field(
        default=None, description="When verdict='supersedes': the node id of the later statement that replaced "
                                  "it, exactly as shown inside its brackets, without the brackets.")
    change_date: Optional[str] = Field(
        default=None, description="When verdict='supersedes': ISO date (YYYY-MM-DD) the new arrangement is first "
                                  "evidenced; the old one ended no later than this.")
    change_date_basis: Optional[str] = Field(
        default=None, description="When verdict='supersedes': which statement and date the change_date comes from.")
    confidence: str = Field(description="'high' | 'medium' | 'low'.")
