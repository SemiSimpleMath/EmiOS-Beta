"""Output schema for subconscious::story_day: what reached the brain on one day, for the owner."""
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="The day in one short sentence.")
    overview: str = Field(description="What came in and what came of it, in three to five sentences.")
