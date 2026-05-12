from typing import List
from pydantic import BaseModel, Field


class PairVerdict(BaseModel):
    """One verdict for a candidate duplicate pair.

    `verdict`:
      - "distinct" → confident these are NOT the same; drop the pair
      - "uncertain" → can't tell from this brief; forward to the full detector

    There is no "merge" verdict at this stage — the cost of a false-merge is
    high enough that the heavy detector + investigator chain owns that call.
    The triage agent's job is only to confidently reject obvious non-matches
    so the downstream cost is spent only on plausible candidates.
    """
    pair_index: int = Field(..., description="1-based index of the pair in the input batch.")
    verdict: str = Field(..., description="'distinct' or 'uncertain'.")
    reason: str = Field("", description="≤1 sentence justification.")


class AgentForm(BaseModel):
    """Batched triage output. One row per input pair, in any order."""
    pairs: List[PairVerdict]
