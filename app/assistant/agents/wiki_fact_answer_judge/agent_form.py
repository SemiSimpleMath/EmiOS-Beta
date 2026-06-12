"""Output schema for wiki::fact_answer_judge.

Interpret the user's answer to a fact-confirmation question."""
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["confirmed", "denied", "corrected", "unclear"] = Field(
        description=(
            "confirmed: the user affirmed the claim as stated. "
            "denied: the user said it's wrong / told us to drop it. "
            "corrected: the user affirmed the substance but fixed details — "
            "put the fixed version in corrected_claim. "
            "unclear: the answer doesn't settle it."
        )
    )
    corrected_claim: Optional[str] = Field(
        default=None, max_length=400,
        description="Required when verdict=corrected: the claim as the user actually stated it.",
    )
    notes: str = Field(
        default="", max_length=300,
        description="One line of reasoning for the audit trail.",
    )
