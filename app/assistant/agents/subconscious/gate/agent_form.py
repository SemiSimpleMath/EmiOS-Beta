"""Output schema for subconscious::gate — one routing decision per inbox event."""
from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(description="The event's label exactly as given, e.g. E3.")
    route: Literal["concern", "new_matter", "none"] = Field(
        description="concern: bears on one or more open concerns. new_matter: raises something no "
                    "open concern covers that the household may need to act on or keep in mind. "
                    "none: bears on no concern and raises nothing worth tracking.")
    concerns: List[str] = Field(
        default_factory=list,
        description="For route=concern: the label of every open concern the event bears on (e.g. C2). "
                    "Empty for the other routes.")
    reasoning: str = Field(description="One sentence: what in the event led to this route.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: List[Decision] = Field(description="Exactly one decision for every event given, in any order.")
