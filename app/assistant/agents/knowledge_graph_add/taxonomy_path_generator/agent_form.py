from pydantic import BaseModel, Field
from typing import List


class AgentForm(BaseModel):
    """Taxonomy path generator output."""
    
    sub_path: List[str] = Field(
        ..., 
        description="List of taxonomy labels UNDER the root (e.g., ['person', 'assistant'] - do NOT include the root!)"
    )
    confidence: float = Field(
        ..., 
        description="Confidence in this classification (0.0-1.0)"
    )
    reasoning: str = Field(
        ..., 
        description="Brief explanation for why this path is appropriate"
    )

