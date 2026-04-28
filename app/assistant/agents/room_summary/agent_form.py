from typing import List

from pydantic import BaseModel, Field


class SummaryPair(BaseModel):
    group_msg_ids: List[str] = Field(
        default_factory=list,
        description="The full list of message IDs being replaced by this summary."
    )
    summary: str = Field(
        description="Concise summary that replaces all messages in group_msg_ids."
    )


class AgentForm(BaseModel):
    summary_pairs: List[SummaryPair] = Field(
        default_factory=list,
        description="Groups of messages to compress into a single summary line."
    )
    hide_ids: List[str] = Field(
        default_factory=list,
        description="Single message IDs to suppress from context."
    )
    pin_ids: List[str] = Field(
        default_factory=list,
        description="Message IDs to keep visible regardless of age or suppression."
    )