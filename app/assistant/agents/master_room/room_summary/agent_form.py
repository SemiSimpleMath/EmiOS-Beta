from typing import List

from pydantic import BaseModel, Field


class SummaryPair(BaseModel):
    msg_id: str = Field(description="The msg_id of the oldest message in the group being summarized.")
    group_msg_ids: List[str] = Field(
        default_factory=list,
        description=(
            "All msg_ids in this group, including the oldest (msg_id). "
            "Every message listed here will be suppressed and replaced by the summary."
        ),
    )
    summary: str = Field(description="Concise summary replacing the entire group of messages.")


class AgentForm(BaseModel):
    summary_pairs: List[SummaryPair] = Field(
        default_factory=list,
        description=(
            "Groups of messages to compress into a single summary line. "
            "Use msg_id of the oldest message in the group. "
            "The summary replaces the whole group in history."
        ),
    )
    hide_ids: List[str] = Field(
        default_factory=list,
        description=(
            "msg_ids to suppress from context. Use for noise: filler acks, "
            "boilerplate system messages, already-handled email notifications, "
            "duplicate/redundant content."
        ),
    )
    pin_ids: List[str] = Field(
        default_factory=list,
        description=(
            "msg_ids to always keep visible regardless of age or suppression. "
            "Use for key decisions, explicit user instructions, 'Remember:' statements."
        ),
    )
