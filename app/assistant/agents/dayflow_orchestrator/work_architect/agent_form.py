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
    kind: Optional[str] = Field(
        default="work",
        description="What this node IS / who runs it: 'work' (default — a worker carries it out) or "
        "'notify' (its ONLY job is to tell the OWNER something one-way; put the message in `detail`; the "
        "system delivers it as a UI notification — no worker, no reply). Use 'notify' for any "
        "'tell/remind/update me' step; use a `user_reply` wake instead when you need an ANSWER back.")
    depends_on: List[str] = Field(
        default_factory=list,
        description="node_ids that must COMPLETE before this node can run (ordering / prerequisites).")
    wake_kind: Optional[str] = Field(
        default=None,
        description="Leave null for a node that runs as soon as its depends_on are done. Otherwise: "
        "'time' (a clock time or fixed delay); 'user_reply' (this node needs the OWNER — the person we "
        "serve — to answer a question or decide; the system notifies them and re-asks until they reply); "
        "'event'/'signal' (an OUTSIDE party or system must act first — someone else's reply, a delivery, "
        "an approval).")
    wake_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime with timezone offset; REQUIRED when wake_kind='time'.")
    wake_ref: Optional[str] = Field(
        default=None,
        description="REQUIRED when wake_kind is user_reply/event/signal. For 'user_reply', write the "
        "clear, friendly QUESTION to ask the owner — it is shown to them verbatim (e.g. 'When does your "
        "fatigue tend to hit, and how have you been sleeping lately?'). For 'event'/'signal', describe "
        "the real-world event being awaited (e.g. 'a reply from the brother to the trip email') — the "
        "state_mover matches it.")
    wait_reason: Optional[str] = Field(
        default=None, description="Human-readable reason this node waits.")

    @model_validator(mode="after")
    def _validate_wake(self):
        if self.kind is not None and self.kind not in {"work", "notify"}:
            raise ValueError("kind must be 'work', 'notify', or null.")
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
