from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class HaltDirective(BaseModel):
    """Evaluator-directed job halt (stop work that is no longer needed)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., description="job_id to cooperatively halt (must be currently running).")
    kind: Literal["moot", "redundant", "superseded", "part_complete"] = Field(
        ...,
        description=(
            "Why to halt: "
            "'moot' (objective invalid), "
            "'redundant' (duplicate work), "
            "'superseded' (better path exists), "
            "'part_complete' (this sub-problem is already solved sufficiently)."
        ),
    )
    reason: str = Field("", description="Short reason/instruction to justify the halt.")


class AgentForm(BaseModel):
    """Structured output for `shared::orchestrator_evaluator`."""

    model_config = ConfigDict(extra="forbid")

    is_done: bool = Field(
        False,
        description="True when the overall orchestrator objective is satisfied OR facts indicate it is moot/invalid.",
    )
    done_reason: str = Field(
        "",
        description="Short reason for completion (or why it is not complete yet).",
    )
    missing_requirements: List[str] = Field(
        default_factory=list,
        description="If not done, concrete missing items that map to additional jobs/stages.",
    )

    halt_directives: List[HaltDirective] = Field(
        default_factory=list,
        description="Jobs to halt now (moot/redundant/superseded/part_complete). If non-empty, replan_needed MUST be true.",
    )

    replan_needed: bool = Field(
        False,
        description="True when the architect should be asked to re-evaluate and potentially spawn/cancel jobs.",
    )
    replan_reason: str = Field(
        "",
        description="Short reason for replanning (what changed / what is missing).",
    )

    notes: str = Field("", description="Short explanation of decisions.")


# Ensure postponed annotations are resolved for structured output schemas.
AgentForm.model_rebuild()

