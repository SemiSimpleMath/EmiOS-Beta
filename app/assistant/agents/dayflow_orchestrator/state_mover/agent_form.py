from typing import List, Literal

from pydantic import BaseModel, Field, model_validator


class StateMutation(BaseModel):
    item_id: int = Field(description="Numeric short_id of the item (e.g. 704).")
    from_state: Literal[
        "new",
        "artifact",
        "needs_planning",
        "important_open",
        "actionable",
        "dispatched",
        "watching",
        "waiting",
        "suppressed",
        "closed",
    ] = Field(description="Observed previous state.")
    to_state: Literal[
        "artifact",
        "needs_planning",
        "important_open",
        "actionable",
        "watching",
        "waiting",
        "suppressed",
        "closed",
    ] = Field(description="Target lifecycle state.")
    reason: str = Field(description="Human-readable transition rationale.")
    wait_reason: str = Field(default="", description="Required when to_state=waiting.")
    reactivate_at: str = Field(default="", description="ISO 8601 datetime with timezone offset. Use the same timezone as shown in Current Time. Example: 2026-04-10T16:55:00-07:00")
    wake_signals: List[str] = Field(default_factory=list, description="Event wake signals.")
    priority_on_wake: str = Field(default="", description="Importance label on wake.")

    @model_validator(mode="after")
    def validate_waiting_rule(self):
        if self.to_state == "waiting":
            has_reactivate = bool(str(self.reactivate_at or "").strip())
            has_signals = bool(self.wake_signals)
            if not self.wait_reason.strip():
                raise ValueError("wait_reason is required when to_state=waiting.")
            if not (has_reactivate or has_signals):
                raise ValueError("to_state=waiting requires reactivate_at or wake_signals.")
        return self


class NodeWake(BaseModel):
    task_id: str = Field(description="The parked work-object node id EXACTLY as shown (work_id::node_id).")
    evidence: str = Field(
        description="The intake content that satisfies the wait — the gist of what arrived — so the "
        "worker resuming the node has what it needs to act.")


class HeldWorkNode(BaseModel):
    task_id: str = Field(description="A READY work-object node you are HOLDING this tick, EXACTLY as shown "
        "in READY WORK NODES (work_id::node_id).")
    hold_reason: str = Field(description="Concrete reason now is the wrong moment to surface this to the "
        "user — e.g. 'quiet hours until 07:00', 'user in a meeting until 15:00', 'user away (idle 90m) — "
        "batch with the next active block'. Never a vague 'maybe later'.")
    reactivate_at: str = Field(default="", description="ISO 8601 local datetime with offset for when to "
        "reconsider promoting it (same timezone as Current Time). Leave empty ONLY if you truly cannot "
        "estimate — it is then re-judged next tick.")


class AgentForm(BaseModel):
    state_mover_summary: str = Field(description="Short summary of applied lifecycle moves.")
    state_mutations: List[StateMutation] = Field(
        default_factory=list,
        description="Lifecycle transitions to apply.",
    )
    node_wakes: List[NodeWake] = Field(
        default_factory=list,
        description="Work-object nodes parked on an external event whose event has CLEARLY arrived in "
        "the recent intake. Omit anything uncertain — it stays parked. Separate from state_mutations.")
    held_work_nodes: List[HeldWorkNode] = Field(
        default_factory=list,
        description="Ready work nodes you are deliberately holding back from dispatch THIS tick. The default "
        "is to PROMOTE every ready node — list one here ONLY when you have a concrete reason it should not "
        "reach the user right now. Omitting a node promotes it. Separate from state_mutations and node_wakes.")
