from typing import List

from pydantic import BaseModel, Field


class NodeWake(BaseModel):
    task: str = Field(description="The parked task's label (T…) under WORK-OBJECT WAITS.")
    source: str = Field(description="The label (S…) of the arrival under RECENT INCOMING that satisfies its wait.")
    evidence: str = Field(
        default="",
        description="Optional: why this arrival satisfies the wait, in words. The worker reads the arrival itself; "
        "the release is what counts. No labels.")


class HeldWorkNode(BaseModel):
    task: str = Field(description="The label (T…) of a READY task you are HOLDING this tick.")
    hold_reason: str = Field(description="Concrete reason now is the wrong moment to surface this to the "
        "user — e.g. 'quiet hours until 07:00', 'user in a meeting until 15:00', 'user away (idle 90m) — "
        "batch with the next active block'. Never a vague 'maybe later'. Words only; no labels.")
    reactivate_at: str = Field(default="", description="ISO 8601 local datetime with offset for when to "
        "reconsider promoting it (same timezone as Current Time). Leave empty ONLY if you truly cannot "
        "estimate — it is then re-judged next tick.")


class AgentForm(BaseModel):
    state_mover_summary: str = Field(default="", description="Optional: one line on what you woke or held, and why. "
                                     "Nothing to say when nothing changes.")
    node_wakes: List[NodeWake] = Field(
        default_factory=list,
        description="Work-object nodes parked on an external event whose event has CLEARLY arrived in "
        "the recent intake. Omit anything uncertain — it stays parked.")
    held_work_nodes: List[HeldWorkNode] = Field(
        default_factory=list,
        description="Ready work nodes you are deliberately holding back from dispatch THIS tick. The default "
        "is to PROMOTE every ready node — list one here ONLY when you have a concrete reason it should not "
        "reach the user right now. Omitting a node promotes it. Separate from node_wakes.")
