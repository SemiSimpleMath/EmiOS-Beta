from typing import List

from pydantic import BaseModel, ConfigDict, Field


class FactPatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(..., description="Facts key to update (shallow patch).")
    value: str | int | float | bool | None = Field(..., description="Facts value (primitive types only).")


class AgentForm(BaseModel):
    """Structured output for `shared::orchestrator_facts_curator`."""

    model_config = ConfigDict(extra="forbid")

    facts_patch: List[FactPatchItem] = Field(
        default_factory=list,
        description="Shallow facts patch as list of {key,value}.",
    )
    is_done: bool = Field(False, description="True if the overall orchestrator task is complete.")
    done_reason: str = Field("", description="Short reason for completion or why not complete yet.")
    missing_requirements: List[str] = Field(
        default_factory=list,
        description="If not done, list concrete missing items that map to new jobs/stages.",
    )
    notes: str = Field(..., description="Short explanation of what changed and why.")


# Ensure postponed annotations are resolved for structured output schemas.
AgentForm.model_rebuild()

