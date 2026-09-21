from typing import List

from pydantic import BaseModel, Field


class NodeWake(BaseModel):
    task_id: str = Field(description="The parked work-object node id EXACTLY as shown (work_id::node_id).")
    source_item_id: str = Field(description="Exact item_id of the matching prepared intake source.")
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
    state_mover_summary: str = Field(description="One line: what you woke, what you held, and why.")
    node_wakes: List[NodeWake] = Field(
        default_factory=list,
        description="Work-object nodes parked on an external event whose event has CLEARLY arrived in "
        "the recent intake. Omit anything uncertain — it stays parked.")
    held_work_nodes: List[HeldWorkNode] = Field(
        default_factory=list,
        description="Ready work nodes you are deliberately holding back from dispatch THIS tick. The default "
        "is to PROMOTE every ready node — list one here ONLY when you have a concrete reason it should not "
        "reach the user right now. Omitting a node promotes it. Separate from node_wakes.")
