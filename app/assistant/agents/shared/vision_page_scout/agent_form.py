from pydantic import BaseModel, Field


class Target(BaseModel):
    """
    A coordinate click target in viewport (CSS pixel) coordinates.
    """

    purpose: str = Field(..., description="What this click is intended to do (e.g., accept_cookies, close_modal).")
    x: float = Field(..., description="Viewport X coordinate (CSS pixels).")
    y: float = Field(..., description="Viewport Y coordinate (CSS pixels).")
    rationale: str = Field(..., description="Brief explanation of why this is the right target.")


class AgentForm(BaseModel):
    """Structured output for `shared::vision_page_scout`."""

    page_overview: str = Field(..., description="2-6 sentences describing the dominant UI elements and layout.")

    suggested_next_steps: list[str] = Field(
        default_factory=list,
        description="Possible steps available for the planner (click button, scroll, write text into a box, toggle, etc).",
    )

    things_to_look_for_in_snapshot: list[str] = Field(
        ...,
        description="Strings/UI targets the planner should search for in the next browser_snapshot.",
    )

    targets: list[Target] = Field(
        default_factory=list,
        description=(
            "Optional coordinate targets (x,y) for obvious UI controls (cookie accept, close modal, primary CTA). "
            "Only include targets you can actually see and are reasonably confident about. Leave empty if unsure."
        ),
    )


# Defensive: resolve postponed annotations in dynamic-import environments.
try:
    AgentForm.model_rebuild()
except Exception:
    pass

