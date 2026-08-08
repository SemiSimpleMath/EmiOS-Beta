"""Structured output for the friction classifier."""
from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    friction: bool = Field(description="True only when the message is meta-feedback about the assistant's own behavior.")
    kind: str = Field(default="other", description="wrong_behavior | repeat_ask | nonsense_question | broken_promise | other")
    quote: str = Field(default="", description="The verbatim span of the user's message carrying the friction.")
    confidence: float = Field(default=0.0, description="0..1 confidence that this is friction about the assistant itself.")
