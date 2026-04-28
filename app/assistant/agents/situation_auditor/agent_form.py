from typing import List, Optional
from pydantic import BaseModel, Field


class Finding(BaseModel):
    category: str = Field(
        description="Type of finding: contradiction, conflict, stale_assumption, schedule_collision, pattern, anomaly"
    )
    summary: str = Field(
        description="One sentence describing what doesn't add up."
    )
    details: str = Field(
        description="2-3 sentences explaining the conflicting signals, which is likely stronger, and why."
    )
    severity: str = Field(
        description="high, medium, or low. High = could cause real problems if ignored."
    )
    suggestion: Optional[str] = Field(
        default=None,
        description="Optional: what should be done about this. Verify with user, dismiss, flag to planner, etc."
    )


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="Your overall assessment of the current situation. What looks coherent, what doesn't."
    )
    findings: List[Finding] = Field(
        description="List of issues found. Empty list if everything looks consistent."
    )
    overall_health: str = Field(
        description="green (everything consistent), yellow (minor inconsistencies), red (significant conflicts)"
    )
