from typing import List

from pydantic import BaseModel, ConfigDict, Field


class ArchitectJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., description="Unique, slug-style ID for this job (e.g., 'nav_to_grubhub').")
    manager_type: str = Field(..., description="Domain-specific manager required (e.g., 'web_manager').")
    sub_task_for_manager: str = Field(..., description="sub-task for this manager.")
    depends_on: List[str] = Field(default_factory=list, description="IDs of jobs that must complete first.")


class AgentForm(BaseModel):
    """Structured output for `shared::orchestrator_architect`."""

    model_config = ConfigDict(extra="forbid")

    spawn: List[ArchitectJob] = Field(default_factory=list, description="DAG jobs for the current phase.")
    notes: str = Field("", description="Short reasoning / status note.")


# Ensure postponed annotations are resolved for structured output schemas.
ArchitectJob.model_rebuild()
AgentForm.model_rebuild()

