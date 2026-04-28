from typing import List, Optional

from pydantic import BaseModel, Field


class ActionableItem(BaseModel):
    fact_summary: str = Field(description="Concise actionable insight.")
    tags: List[str] = Field(description="Tags for routing to resource files.")
    change_recommended: str = Field(description="Specific change or recommendation.")
    evidence: List[str] = Field(
        default_factory=list,
        description="Short evidence strings or ids from the timeline.",
    )
    temporal_scope: Optional[str] = Field(
        default="chronic",
        description="chronic | daily | historical",
    )


class AgentForm(BaseModel):
    actionable_information: List[ActionableItem]
