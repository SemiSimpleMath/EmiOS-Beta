from typing import List
from pydantic import BaseModel, ConfigDict, Field


class ExtractableLine(BaseModel):
    """One user message marked as containing claim-bearing autobiographical content."""
    model_config = ConfigDict(extra="forbid")

    user_message_index: int = Field(
        ...,
        description="0-based index into the user_lines list shown in the prompt.",
    )
    reason: str = Field(
        ...,
        description="Short justification why this line is extractable (<= 15 words).",
    )


class AgentForm(BaseModel):
    """Line-item-veto extractability verdict.

    The v1 critic emitted a binary `extractable: bool` for the whole window; if
    even one user line was usable, the entire window flowed to the extractor —
    which then over-extracted jokes, hypotheticals, and comparisons. v2 lets
    the critic name exactly which user lines should yield claims. Non-listed
    user lines stay visible to the extractor as context (for anaphora /
    discourse coherence) but the extractor is instructed not to mint claims
    from them.
    """
    model_config = ConfigDict(extra="forbid")

    extractable_lines: List[ExtractableLine] = Field(
        default_factory=list,
        description=(
            "User messages that carry claim-bearing autobiographical content. "
            "Empty list = nothing in this window is worth extracting (reject)."
        ),
    )
    overall_reason: str = Field(
        ...,
        description="One-line summary of the decision (<= 25 words).",
    )
