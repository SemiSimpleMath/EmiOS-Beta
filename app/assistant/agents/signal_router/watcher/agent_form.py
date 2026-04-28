from pydantic import BaseModel, Field
from typing import List


class AgentForm(BaseModel):
    should_emit_event: bool = Field(
        description="True when the signal satisfies the watch predicate and an event should be emitted."
    )
    match_reason: str = Field(
        description="Short reason for match or non-match."
    )
    confidence: float = Field(
        description="Confidence in range [0.0, 1.0]."
    )
    dedupe_key_hint: str = Field(
        description="Stable dedupe token candidate (for example message id, thread id, or normalized content hash basis)."
    )
    evidence_lines: List[str] = Field(
        default_factory=list,
        description="Short factual evidence lines from the signal/predicate. Max 5 lines."
    )
    payload_summary: str = Field(
        default="",
        description="Short optional payload summary text for downstream consumers."
    )
