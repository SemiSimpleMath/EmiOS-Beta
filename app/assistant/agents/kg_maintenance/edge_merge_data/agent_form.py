from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    reasoning: str = Field(..., description="Explanation for the merge decision.")
    merged_relationship_type: str = Field(..., description="Relationship type for the merged edge.")
    sentence: str = Field(..., description="Combined or representative sentence.")
    context_window: str = Field(..., description="Combined or representative context window.")
    importance: float = Field(..., ge=0.0, le=1.0, description="Representative importance score.")
    credibility: float = Field(..., ge=0.0, le=1.0, description="Credibility after adjustment.")
    start_date: str
    end_date: str
    valid_during: str = Field(..., description="Merged valid duration (usually from most recent edge).")
