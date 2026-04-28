from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """Structured output for `shared::orchestrator_done_checker`."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["done"] = Field(..., description="Always 'done'.")
    is_done: bool = Field(..., description="True if the orchestrator can stop.")
    done_reason: str = Field(..., description="Short explanation for why we are done/not done.")
    missing_requirements: List[str] = Field(
        default_factory=list,
        description="If not done, list concrete missing items needed to complete the task.",
    )


# Ensure postponed annotations are resolved for structured output schemas.
AgentForm.model_rebuild()

