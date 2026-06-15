from typing import List, Optional
from pydantic import BaseModel, Field


class PlanSynopsis(BaseModel):
    plan_id: str = Field(description="Stable plan identifier.")
    is_new: bool = Field(description="True if this is a brand new plan being created this pass. False if this plan already exists.")
    changed: bool = Field(description="True ONLY if you are creating this plan or changing its objective, synopsis, criteria, or steps. False if unchanged.")
    objective: str = Field(description="Clear one-line objective for the plan.")
    synopsis: str = Field(description="Plain-language strategy summary.")
    success_criteria: str = Field(default="", description="How to know the plan is complete.")
    step_outline: List[str] = Field(
        default_factory=list,
        description="Ordered high-level outline of the plan.",
    )
    based_on: List[str] = Field(
        default_factory=list,
        description=(
            "Provenance — ids of the sources this plan is based on (artifact_id, plan_id, a concern, a "
            "belief, etc.). Leave EMPTY if it is your own original observation; the plan's own id then "
            "stands as the origin. Re-state these whenever you change the plan so its provenance is not lost."
        ),
    )


class PlannedTask(BaseModel):
    task_id: str = Field(description="Reference label for this task. Use the short numeric id shown in plan status for existing tasks. For new tasks use any short label — the system will assign a canonical id.")
    plan_id: Optional[str] = Field(default=None, description="REQUIRED for plan tasks. The parent plan id this task belongs to. Null only for standalone tasks with no parent plan.")
    task: str = Field(description="High-level task intent, constraints, and expected outcome.")
    based_on: List[str] = Field(
        default_factory=list,
        description=(
            "Provenance — ids of the sources this task is based on: the artifact_id of an email/item "
            "you are acting on, a plan_id, a subconscious concern, a belief, etc. Leave EMPTY if this is "
            "your OWN original observation that no surfaced source prompted (e.g. 'it's summer and hot — "
            "make sure the AC works') — the task's own id then stands as a new origin. Cite only sources "
            "you are genuinely acting on."
        ),
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="List of task ids that this task depends on, if any.",
    )
    reactivate_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime with timezone offset for when this task should wake. Use the same timezone as shown in Current Time. Example: 2026-04-10T16:55:00-07:00",
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
        description="Only include plans you are CREATING or CHANGING this pass. Do not echo back unchanged plans.",
    )
    completed_plan_ids: List[str] = Field(
        default_factory=list,
        description="Plan IDs whose objective is fully met or obsolete.",
    )
    closed_task_ids: List[str] = Field(
        default_factory=list,
        description="Individual task IDs to close because they are duplicates, obsolete, or wrong.",
    )