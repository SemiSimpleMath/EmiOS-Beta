from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Structured output for `ring_analyzer`."""

    caption: str = Field(
        ...,
        description="One short sentence describing what is visible in the frame.",
    )
    is_significant: bool = Field(
        ...,
        description="True if this frame likely warrants user attention (visitor, package, unusual event).",
    )
    significance_reason: str = Field(
        default="",
        description="Short reason when is_significant=True; empty string otherwise.",
    )
