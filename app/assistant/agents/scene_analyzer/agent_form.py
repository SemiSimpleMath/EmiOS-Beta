from typing import Literal

from pydantic import BaseModel, Field


# Category vocabulary for door-style cameras. The active set per camera
# is enumerated in that camera's skill (e.g. front-door-watch SKILL.md
# Category rubric section). Unknown is the catch-all when the frame
# doesn't fit any other category.
SceneCategory = Literal[
    "food_delivery",
    "package_delivery",
    "household_out_with_dogs",
    "kids_leaving",
    "family_arriving",
    "car_by",
    "wind",
    "stranger_lingering",
    "unknown",
    "emergency",
]


class AgentForm(BaseModel):
    """Structured output for the generic camera scene analyzer.

    Per-camera knowledge (what's normal at this view, what counts as which
    category, who's expected) arrives via skills_input — the schema stays
    intentionally lightweight so it works for any non-sleep camera.
    """

    caption: str = Field(
        ...,
        description="One short sentence describing what is visible in the frame.",
    )
    category: SceneCategory = Field(
        default="unknown",
        description=(
            "The category this frame falls into per the camera's skill rubric. "
            "Drives downstream escalation routing. Use 'unknown' only when the "
            "frame genuinely doesn't fit any other category — never as a hedge."
        ),
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
