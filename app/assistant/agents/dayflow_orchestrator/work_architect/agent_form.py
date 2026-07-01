"""Output contract for the dayflow WORK ARCHITECT (Part 2 of the split planner).

The steward (strategic_planner_wo) decides WHAT work exists (creates a goal). The architect decides how
that goal is STRUCTURED: it decomposes the goal into a small DAG of work nodes, with dependencies AND —
the part the orchestrator_architect lacks — WAIT-GATES for steps that must pause for a future time or an
external event (a reply, a delivery, an approval). The worker (work_emi_team) executes each node; the
state_mover judges the wait-gates. The architect authors TWO wake primitives — wake_at (a deterministic
time) | wake_ref (a prose condition the state_mover matches against reality); work_architect_apply derives
the substrate wake_kind. Whether a node is work / a notify / an ask is NOT the architect's call — it writes
the node's GOAL, and the switchboard reads that goal and decides how to reach it.
"""
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class WorkNode(BaseModel):
    node_id: str = Field(
        description="Short slug, unique within this graph, referenced by other nodes' depends_on "
        "(e.g. 'email_brother'). Reflect the node's job, not a vague verb.")
    title: str = Field(description="Short title of this unit of work.")
    detail: str = Field(
        description="What this node must accomplish — the objective. The worker decides HOW and which "
        "tools/managers to use; do NOT write step-by-step instructions here.")
    depends_on: List[str] = Field(
        default_factory=list,
        description="node_ids that must COMPLETE before this node can run (ordering / prerequisites).")
    wake_at: Optional[str] = Field(
        default=None,
        description="DETERMINISTIC wake — an ISO 8601 datetime with timezone offset. The node parks until this "
        "exact clock time (a specific time, or now + a fixed delay: add the delay to the CURRENT TIME in the "
        "prompt). ELAPSED TIME IS ALWAYS THIS — never model 'N hours/days later' as an event.")
    wake_ref: Optional[str] = Field(
        default=None,
        description="PROSE wake — the node parks until this natural-language condition is met: an OUTSIDE "
        "event someone/something else must produce ('a reply from the brother to the trip email', 'the "
        "package is delivered', 'an approval lands'). The state_mover matches it against reality.")
    wait_reason: Optional[str] = Field(
        default=None, description="Human-readable reason this node waits.")

    @model_validator(mode="after")
    def _validate_wake(self):
        if str(self.wake_at or "").strip() and str(self.wake_ref or "").strip():
            raise ValueError("a node waits on a time (wake_at) OR a condition (wake_ref), not both.")
        return self


class AgentForm(BaseModel):
    architect_summary: str = Field(description="One line on the graph you designed.")
    nodes: List[WorkNode] = Field(
        default_factory=list,
        description="The DAG of work nodes for this goal. Keep it lean — 1-5 nodes for most goals.")
    abandon_node_ids: List[str] = Field(
        default_factory=list,
        description="RE-PLAN ONLY (leave empty on a fresh decomposition): node_ids of EXISTING nodes the "
        "latest situation makes moot or wrong — each one AND its un-finished sub-steps are abandoned. Use "
        "this to PRUNE a dead branch, not only to add. E.g. evidence showed the unit is under warranty, so "
        "abandon the 'find_contractor' branch and add a 'contact_manufacturer' path. Already-finished "
        "(done/closed) nodes are left as a record; only un-done moot work is pruned.")
