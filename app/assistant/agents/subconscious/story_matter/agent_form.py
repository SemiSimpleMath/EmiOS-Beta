"""Output schema for subconscious::story_matter: one matter told to the owner."""
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(description="The event's label as given (E1, E2, ...).")
    line: str = Field(description="One sentence: what arrived and what became of it.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="What this matter was about, in one short sentence.")
    lines: List[Line] = Field(description="One line for every event, in the order given.")
