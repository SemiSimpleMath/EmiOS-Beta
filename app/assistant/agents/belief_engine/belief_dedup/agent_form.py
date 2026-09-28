from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    verdict: Literal["same", "refines", "contradicts", "new"] = Field(
        description="How the new belief relates to the beliefs already held, as defined in the instructions.")
    target: Optional[str] = Field(
        default=None,
        description="For same, refines and contradicts: the id of the ONE existing belief, copied "
                    "exactly. Empty for new.")
    sources: List[int] = Field(
        default_factory=list,
        description="For same and contradicts: the numbers (n) of the new belief's sources that bear on "
                    "the existing belief — the ones it gains. Empty for refines and new.")
    reasoning: str = Field(
        description="Two sentences quoting the words from both sources that decided the verdict.")
