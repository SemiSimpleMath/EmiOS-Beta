from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Structured output for the generic camera scene analyzer.

    Per-camera knowledge (what's normal at this view, what to watch for,
    who's expected) arrives via skills_input — the schema stays
    intentionally lightweight so it works for any non-sleep camera.
    """

    caption: str = Field(
        ...,
        description="One short sentence describing what is visible in the frame.",
    )
    is_significant: bool = Field(
        ...,
        description="True if this frame likely warrants user attention given the camera's role and context.",
    )
    significance_reason: str = Field(
        default="",
        description="Short reason when is_significant=True; empty string otherwise.",
    )
    importance: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "0-10 integer score for how worth reviewing this frame is when "
            "scanning many. Bias toward the lower end; reserve 6+ for genuinely "
            "notable frames."
        ),
    )
