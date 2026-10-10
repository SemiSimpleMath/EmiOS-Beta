"""Output schema for subconscious::concern_summary: a concern's older notes folded into one account."""
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="The concern as it stands, from the previous summary and the notes: what is "
                                     "known now, what was done and how it went, what the owner said in the owner's "
                                     "words, and what is still open. Each fact with its date and where it came from. "
                                     "In words; no labels.")
