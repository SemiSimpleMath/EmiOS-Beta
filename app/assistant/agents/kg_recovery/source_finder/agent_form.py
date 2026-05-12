from typing import List
from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Source-message identification for a legacy KG node with no provenance.

    Given the node's prose and a list of candidate `unified_log_2026`
    messages (already pre-filtered by content + date heuristics), pick
    the message id(s) that ORIGINATED this fact — the user actually
    said/asserted it there, or the assistant's reply made it true.

    Returning an empty list with confidence='low' is the correct answer
    when none of the candidates plausibly originate the fact. Better to
    leave the node orphan than guess.
    """

    chosen_message_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Full unified_log_2026 message ids that originate this node's fact. "
            "Empty list if no candidate is a confident match. "
            "Usually 1-3 ids: the primary asserting message + tightly adjacent "
            "reinforcing messages. NEVER include unrelated messages just because "
            "they share a keyword."
        ),
    )
    confidence: str = Field(
        ...,
        description="One of: 'high' (clear match), 'medium' (plausible but not certain), 'low' (no match — chosen_message_ids should be empty).",
    )
    reasoning: str = Field(
        ...,
        description=(
            "Short explanation (<= 60 words) for the chosen ids OR for why no "
            "candidate matched. Cite specific message content, not just keyword overlap."
        ),
    )
