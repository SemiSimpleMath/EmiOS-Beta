"""Output contract for the WORK-OBJECT strategic planner (dayflow Phase 1).

The planner is demoted from task-decomposer to PORTFOLIO STEWARD: it mints work-object GOALS and
decides which to advance / complete / abandon. It does NOT emit tasks — work_emi_team decomposes each
goal's graph internally. Replaces strategic_planner's PlanSynopsis/PlannedTask. The per-task fields
(dependencies / reactivate_at / wake_signals) are gone: they live on the graph now (depends_on edges,
node wake_at/wake_kind). See scratch/DAYFLOW_WORKOBJECT_SCOPING.md.
"""
from typing import List

from pydantic import BaseModel, Field


class WorkObjectSpec(BaseModel):
    work_id: str = Field(
        description="Stable work-object id. Use the id shown in the portfolio for an EXISTING work "
        "object whose objective/criteria you are changing; leave EMPTY ('') to CREATE a new one.")
    objective: str = Field(
        description="The goal — one clear sentence of what this work object should achieve. This becomes "
        "the goal node; work_emi_team decomposes it into the graph. Do NOT list steps/tasks here.")
    success_criteria: str = Field(default="", description="How to know the objective is fully met.")
    based_on: List[str] = Field(
        default_factory=list,
        description="Provenance — ids of the sources prompting this (artifact_id of an email/item, a "
        "subconscious concern, a belief). Leave EMPTY if it is your own original observation.")


class AgentForm(BaseModel):
    planner_summary: str = Field(description="Short summary of your steward decisions this pass.")
    new_or_changed: List[WorkObjectSpec] = Field(
        default_factory=list,
        description="Work objects to CREATE (empty work_id) or whose objective/criteria you are CHANGING. "
        "Do not echo back unchanged work objects.")
    advance_work_ids: List[str] = Field(
        default_factory=list,
        description="Work-object ids to push forward THIS cycle (hand to work_emi_team to advance). Only "
        "those that are ready and worth advancing now — not ones that are waiting/blocked or already done.")
    complete_work_ids: List[str] = Field(
        default_factory=list,
        description="Work-object ids whose objective is fully met or obsolete — close them.")
    abandon_work_ids: List[str] = Field(
        default_factory=list,
        description="Work-object ids that turned out wrong/unwanted — drop them.")
