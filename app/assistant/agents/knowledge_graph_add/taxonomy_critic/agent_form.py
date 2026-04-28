"""
Taxonomy Critic Agent Form

Reviews multiple candidate paths from both LLM-guided and beam search methods.
Decides if any candidate is acceptable, or if all should be rejected for path correction.
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional


class AgentForm(BaseModel):
    """Output schema for taxonomy critic reviewing multiple candidates."""
    
    decision: Literal["APPROVE", "REJECT"] = Field(
        description=(
            "The critic's decision:\n"
            "- APPROVE: One of the candidate paths is acceptable and can be implemented\n"
            "- REJECT: None of the candidates are acceptable, needs path correction"
        )
    )
    
    approved_candidate_rank: Optional[int] = Field(
        default=None,
        description=(
            "If APPROVE: The rank (1-based) of the approved candidate from the list.\n"
            "If REJECT: Leave as None."
        )
    )
    
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this decision (0.0-1.0)"
    )
    
    reasoning: str = Field(
        description=(
            "Brief explanation of the decision:\n"
            "- For APPROVE: Why the selected candidate is acceptable\n"
            "- For REJECT: Why none of the candidates are acceptable (path corrector will find better)"
        )
    )


