from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Binary extractability gate for a conversation window."""
    extractable: bool = Field(
        ...,
        description="True if the window contains any biographical signal worth sending to the fact extractor.",
    )
    reason: str = Field(
        ...,
        description="Short justification (<= 20 words).",
    )
