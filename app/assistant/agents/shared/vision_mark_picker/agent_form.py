from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """
    Structured output for `shared::vision_mark_picker`.
    """

    model_config = ConfigDict(extra="forbid")

    # Use built-in `list[...]` instead of typing.List to avoid "List not defined"
    # under postponed annotations + dynamic imports.
    mark_ids: list[int] = Field(..., description="1-3 mark ids (integers) corresponding to the numbered overlays.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence 0-1.")
    rationale: str = Field(..., description="Brief explanation of why these marks match the task.")


# Defensive: force Pydantic to resolve postponed annotations in dynamic-import environments.
try:
    AgentForm.model_rebuild()
except Exception:
    pass

