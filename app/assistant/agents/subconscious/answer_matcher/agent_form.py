"""Output schema for subconscious::answer_matcher.

Given ONE question the assistant asked the user and the user messages
that followed in the same room, decide whether the question has been
answered.
"""
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["answered", "partial", "no_answer"] = Field(
        description=(
            "answered: a candidate message clearly answers the question. "
            "partial: the user engaged with the topic but didn't supply the "
            "asked-for information yet (keep watching). "
            "no_answer: the messages don't address the question at all."
        )
    )
    answer_message_id: Optional[str] = Field(
        default=None,
        description="The id of the message containing the answer. Required when verdict=answered.",
    )
    answer_text: Optional[str] = Field(
        default=None,
        max_length=600,
        description=(
            "The answer, as close to the user's own words as possible "
            "(quote or near-quote, plus any disambiguation like which date "
            "they meant). Required when verdict=answered."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="How sure you are about the verdict."
    )
    notes: str = Field(
        default="",
        max_length=300,
        description="One line of reasoning, for the audit trail.",
    )
