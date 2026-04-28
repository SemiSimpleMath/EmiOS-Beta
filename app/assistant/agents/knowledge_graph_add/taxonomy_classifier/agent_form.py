from pydantic import BaseModel, Field
from typing import Optional


class AgentForm(BaseModel):
    taxonomy_id: Optional[int] = Field(None, description="ID of the selected taxonomy type (null if no match)")
    confidence: float = Field(..., description="Confidence score 0.0-1.0")
    match_quality: int = Field(..., description="Match quality rating 1-10")
    new_subcategory_suggestion: Optional[str] = Field(None, description="Suggested new subcategory name")
    reasoning: str = Field(..., description="Brief explanation for the choice")
