from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="Your step-by-step thinking: what is this item, what category, who might it involve, what did the KG results tell you."
    )
    enrichment: str = Field(
        description="1-3 sentence annotation for downstream agents. Empty string if no enrichment needed."
    )
    confidence: str = Field(
        description="high, medium, or low — how confident are you in the enrichment."
    )
