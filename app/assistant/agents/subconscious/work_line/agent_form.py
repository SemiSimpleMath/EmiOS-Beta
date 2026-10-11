"""Output schema for subconscious::work_line: the one line an ended work object is known by."""
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: str = Field(description="One sentence of what came of the work: what was done or found and when, and what "
                                  "was left undone when that matters. In words; no labels.")
