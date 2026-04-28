from typing import List, Optional

from pydantic import BaseModel, Field


class PlanSynopsis(BaseModel):
    plan_id: str = Field(description="Stable plan identifier.")
    objective: str = Field(description="Clear one-line objective for the plan.")
    synopsis: str = Field(description="Plain-language strategy summary.")
    success_criteria: str = Field(default="", description="How to know the plan is complete.")
    step_outline: List[str] = Field(
        default_factory=list,
        description="Ordered high-level outline of the plan.",
    )


class PlannedTask(BaseModel):
    task_id: str = Field(description="Stable task id. Prefer format task:<plan_id>:<n>.")
    plan_id: Optional[str] = Field(default=None, description="Related plan id. Null for standalone task.")
    task: str = Field(description="High-level task intent, constraints, and expected outcome.")
    dependencies: List[str] = Field(
        default_factory=list,
        description="List of task ids that this task depends on, if any.",
    )
    reactivate_at: Optional[str] = Field(
        default=None,
        description="Local datetime wake time when this task should become eligible again, if applicable.",
    )
    wait_reason: Optional[str] = Field(
        default=None,
        description="Reason this task should remain inactive until a later time or condition, if applicable.",
    )
    wake_signals: List[str] = Field(
        default_factory=list,
        description="Event-based wake triggers for this task, if applicable.",
    )


class AgentForm(BaseModel):
    planner_summary: str = Field(description="Short summary of planning decisions this pass.")
    planned_tasks: List[PlannedTask] = Field(
        default_factory=list,
        description="Tasks created or updated by planner.",
    )
    plan_synopses: List[PlanSynopsis] = Field(
        default_factory=list,
        description="Plan overviews for active plans.",
    )
    completed_plan_ids: List[str] = Field(
        default_factory=list,
        description="Plan IDs whose objective is fully met or obsolete.",
    )
    cancelled_task_ids: List[str] = Field(
        default_factory=list,
        description="Individual task IDs to cancel because they are duplicates, obsolete, or wrong.",
    )

