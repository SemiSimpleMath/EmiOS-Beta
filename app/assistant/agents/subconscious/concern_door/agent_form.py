"""Output schema for subconscious::concern_door — one decision per candidate concern."""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(description="The candidate's label exactly as given, e.g. N2.")
    decision: Literal["new", "same_open", "same_closed", "same_candidate"] = Field(
        description="new: no concern given covers this matter. same_open: an open concern (active or "
                    "addressing) already tracks this same matter. same_closed: a closed concern (resolved "
                    "or dormant) already settled this same matter and nothing since reopens it. "
                    "same_candidate: another candidate in this batch is the same matter.")
    same_as: Optional[str] = Field(
        default=None,
        description="For same_open / same_closed: the concern's label (e.g. C4). For same_candidate: the "
                    "other candidate's label, which must itself be decided new. Empty for new.")
    reason: str = Field(description="One sentence: what makes it the same matter, or a different one.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: List[Decision] = Field(description="Exactly one decision for every candidate given.")
