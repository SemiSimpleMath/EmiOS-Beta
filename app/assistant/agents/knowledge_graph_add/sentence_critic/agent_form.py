from pydantic import BaseModel, Field
from typing import List


class SentenceVerdict(BaseModel):
    index: int = Field(..., description="Zero-based index of the sentence in the input list")
    sentence: str = Field(..., description="The sentence being judged")
    verdict: str = Field(..., description="KEEP or REJECT")
    reason: str = Field(..., description="One-sentence justification")


class AgentForm(BaseModel):
    """Batch verdict: one decision per parsed sentence."""
    verdicts: List[SentenceVerdict] = Field(
        ...,
        description="One entry per sentence, same order as input",
    )
