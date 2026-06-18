from typing import List

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """Structured output for `worker_agents::curator` — the WorkObject done/moot judge.

    Modeled on `shared::orchestrator_facts_curator` (is_done / done_reason /
    missing_requirements), with `moot` made EXPLICIT because the WorkOrchestrator's
    curate hook aborts the whole WorkObject on moot. `facts_patch` is dropped — the
    graph already holds the facts (Evidence nodes)."""

    model_config = ConfigDict(extra="forbid")

    is_done: bool = Field(False, description="True if the goal is satisfied by the evidence so far (good-enough counts).")
    moot: bool = Field(False, description="True if the goal's PREMISE is invalid/impossible/no-longer-applicable, or already satisfied elsewhere — abandon, don't keep working.")
    done_reason: str = Field("", description="Short reason for is_done or moot (or why neither yet).")
    missing_requirements: List[str] = Field(
        default_factory=list,
        description="If neither done nor moot, the concrete missing pieces still needed.",
    )
    notes: str = Field("", description="Short explanation of the judgment.")


AgentForm.model_rebuild()
