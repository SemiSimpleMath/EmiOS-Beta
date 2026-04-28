from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """
    Structured output for `shared::vision_prose_scout`.

    Prose-only: no coordinate targets.
    """

    page_overview: str = Field(..., description="5-12 sentences describing visible UI elements and layout.")
    blockers: list[str] = Field(default_factory=list, description="Any overlays/modals intercepting clicks.")
    suggested_next_steps: list[str] = Field(
        default_factory=list,
        description="Concrete next steps for the planner (no coords).",
    )
    things_to_look_for_in_snapshot: list[str] = Field(
        default_factory=list,
        description="Strings/UI targets the planner should search for in the next browser_snapshot.",
    )


# Defensive: resolve postponed annotations in dynamic-import environments.
try:
    AgentForm.model_rebuild()
except Exception:
    pass

