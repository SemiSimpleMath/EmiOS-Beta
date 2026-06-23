"""Output contract for the dayflow EVALUATOR (formerly the work-object steward).

The evaluator does NOT decompose and does NOT execute. Every tick it judges the PORTFOLIO of work
objects + new intake and decides only WHAT needs to change: create a new work object (incl. a one-step
one), change an existing objective, flag one for re-planning, or complete/abandon it. The architect
(work_architect) decomposes/re-plans each flagged work object one at a time; work_emi_team executes.

`advance` is gone — work_execution runs every ready node deterministically, it never gated on it.
`replan_work_ids` is new — it asks the architect to re-plan an existing work object whose graph no
longer fits (off-track / situation changed / stalled). Per-step fields (dependencies / reactivate_at /
wake_signals) live on the graph (depends_on edges, node wake_at/wake_kind), set by the architect.
"""
from typing import List

from pydantic import BaseModel, Field


class WorkObjectSpec(BaseModel):
    work_id: str = Field(
        description="Stable work-object id. Use the id shown in the portfolio for an EXISTING work "
        "object whose objective/criteria you are changing; leave EMPTY ('') to CREATE a new one.")
    objective: str = Field(
        description="The goal — one clear sentence of what this work object should achieve, from a "
        "one-step action (e.g. 'set the home AC to 70F tonight at 21:00') up to multi-step work. This "
        "becomes the goal node; the architect decomposes it into the graph. Do NOT list steps here.")
    success_criteria: str = Field(default="", description="How to know the objective is fully met.")
    based_on: List[str] = Field(
        default_factory=list,
        description="Provenance — ids of the sources prompting this (artifact_id of an email/item, a "
        "subconscious concern, a belief). Leave EMPTY if it is your own original observation.")


class AgentForm(BaseModel):
    evaluation_summary: str = Field(description="Short summary of your evaluation decisions this pass.")
    new_or_changed: List[WorkObjectSpec] = Field(
        default_factory=list,
        description="Work objects to CREATE (empty work_id) — including one-step ones — or whose "
        "objective/criteria you are CHANGING. Do not echo back unchanged work objects.")
    replan_work_ids: List[str] = Field(
        default_factory=list,
        description="Existing work-object ids whose current graph no longer fits (off-track, situation "
        "changed, stalled) — ask the architect to RE-PLAN them. You do not write the new steps. Do NOT "
        "list ids that are simply progressing normally; those re-plan on their own as work completes.")
    complete_work_ids: List[str] = Field(
        default_factory=list,
        description="Work-object ids whose objective is fully met or obsolete — close them.")
    abandon_work_ids: List[str] = Field(
        default_factory=list,
        description="Work-object ids that turned out wrong/unwanted, or that the user declined — drop them.")
