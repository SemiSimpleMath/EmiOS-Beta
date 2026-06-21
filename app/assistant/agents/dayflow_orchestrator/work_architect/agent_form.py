"""Output contract for the dayflow WORK ARCHITECT (Part 2 of the split planner).

The steward (strategic_planner_wo) decides WHAT work exists (creates a goal). The architect decides how
that goal is STRUCTURED: it decomposes the goal into a small DAG of work nodes, with dependencies AND —
the part the orchestrator_architect lacks — WAIT-GATES for steps that must pause for a future time or an
external event (a reply, a delivery, an approval). The worker (work_emi_team) executes each node; the
state_mover fires the wait-gates. Node fields map onto the substrate: depends_on edge, wake_kind/wake_at/
wake_ref (see work_objects/model.py WAKE_KINDS = time | event | user_reply | signal).
"""
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

_WAKE_KINDS = {"time", "event", "user_reply", "signal"}


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
    wake_kind: Optional[str] = Field(
        default=None,
        description="Leave null for a node that runs as soon as its depends_on are done. Set to "
        "'time' | 'user_reply' | 'event' | 'signal' when this node must PARK and wait for an external "
        "condition before it runs.")
    wake_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime with timezone offset; REQUIRED when wake_kind='time'.")
    wake_ref: Optional[str] = Field(
        default=None,
        description="What is being waited on, in words (e.g. 'a reply from the brother to the trip "
        "email'); REQUIRED when wake_kind is event/user_reply/signal. The state_mover matches the "
        "real-world event to this description.")
    wait_reason: Optional[str] = Field(
        default=None, description="Human-readable reason this node waits.")

    @model_validator(mode="after")
    def _validate_wake(self):
        if self.wake_kind is None:
            return self
        if self.wake_kind not in _WAKE_KINDS:
            raise ValueError(f"wake_kind must be one of {_WAKE_KINDS} or null.")
        if self.wake_kind == "time" and not str(self.wake_at or "").strip():
            raise ValueError("wake_at is required when wake_kind='time'.")
        if self.wake_kind in {"event", "user_reply", "signal"} and not str(self.wake_ref or "").strip():
            raise ValueError("wake_ref is required when wake_kind is event/user_reply/signal.")
        return self


class AgentForm(BaseModel):
    architect_summary: str = Field(description="One line on the graph you designed.")
    nodes: List[WorkNode] = Field(
        default_factory=list,
        description="The DAG of work nodes for this goal. Keep it lean — 1-5 nodes for most goals.")
